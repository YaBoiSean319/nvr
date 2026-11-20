"""Every way out of the live video websocket, pinned before ``live_stream`` is split (#286).

``live_stream`` (app/api/live_media.py) is one long handler: refusals before a stream is joined, a stream that fails or carries video the
browser cannot play, the audio messages, access that ends while watching, a viewer too slow to keep up, and the slots it must give back on
every path. The tests in tests/test_live_stream.py need a simulated camera; these replace only the camera's shared stream (the hub's
``join``) with one the test scripts, so each path runs anywhere, in milliseconds, through the real handler, sign-in and slot counting.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import privacy
from app.api import live_media
from app.application import create_app
from app.credentials import encrypt
from app.store import connect
from tests.test_support import TEST_PASSWORD

RTSP = "rtsp://192.0.2.10:554/stream"
H264 = 'video/mp4; codecs="avc1.640028"'
H264_AAC = 'video/mp4; codecs="avc1.640028, mp4a.40.2"'
HEVC = 'video/mp4; codecs="hvc1.1.6.L120.90"'


class ScriptedStream:
    """What the hub hands the handler: ready or failed, a MIME type, an initialization segment, and the items its viewer will receive."""

    def __init__(self, *, ready=True, failed=None, mime=H264, items=(), closed_reason=None, timing=None):
        self.ready = asyncio.Event()
        self.failed, self.mime, self.init, self.accelerated = failed, mime, b"INIT", False
        self.items, self.closed_reason, self.timing = list(items), closed_reason, timing or {"spawned_ms": 7}
        self.make_ready = ready
        self.joined: dict | None = None


class LiveStreamPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH")}
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        self.app = create_app(start_workers=False)
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()
        self.assertEqual(self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 201)
        self.camera("cam", "h264")
        self.left: list = []
        self.stream = ScriptedStream()
        hub = self.app.state.live_hub
        test = self

        def join(key, source, include_audio, viewer, on_fragment=None, video_filter=None, transcode=False, hevc_copy=False):
            stream = test.stream
            stream.joined = {"key": key, "source": source, "include_audio": include_audio, "video_filter": video_filter, "transcode": transcode, "hevc_copy": hevc_copy}
            loop = asyncio.get_running_loop()
            if stream.make_ready:
                stream.ready.set()
            for item in stream.items:
                loop.call_soon(viewer.queue.put_nowait, item)
            viewer.closed_reason = stream.closed_reason
            return stream

        for patcher in (patch.object(hub, "join", join), patch.object(hub, "leave", lambda stream, viewer: self.left.append(stream))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.context.__exit__(None, None, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def camera(self, camera_id: str, codec: str, *, url: str = RTSP, audio: bool = False) -> None:
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,video_codec,audio_enabled,audio_supported) VALUES(?,?,?,'motion',?,?,?)",
                       (camera_id, f"Camera {camera_id}", encrypt(url), codec, int(audio), int(audio)))

    def closing(self, websocket) -> tuple[list, int]:
        """Everything the server sends until it closes the socket, and the close code."""
        received = []
        try:
            while True:
                message = websocket.receive()
                if message["type"] == "websocket.close":
                    return received, message.get("code", 1000)
                received.append(message.get("text") or message.get("bytes"))
        except WebSocketDisconnect as closed:
            return received, closed.code

    def assert_everything_given_back(self):
        deadline = time.monotonic() + 3
        while (live_media._live_view_count or live_media._live_audio_count or live_media._transcodes) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual((live_media._live_view_count, live_media._live_audio_count, dict(live_media._transcodes)), (0, 0, {}))

    def refusal(self, path: str) -> tuple[dict, int]:
        with self.client.websocket_connect(path) as websocket:
            received, code = self.closing(websocket)
        return json.loads(received[0]), code

    # --- before a stream is joined -------------------------------------------------------------------------------------------------

    def test_a_camera_whose_stream_address_is_not_rtsp_is_refused_and_takes_no_slot(self):
        self.camera("bad", "h264", url="http://192.0.2.11/video")
        message, code = self.refusal("/api/cameras/bad/live/stream")
        self.assertEqual((message, code), ({"type": "error", "detail": "Camera stream configuration is invalid"}, live_media.CLOSE_STREAM_FAILED))
        self.assertIsNone(self.stream.joined)
        self.assert_everything_given_back()

    def test_a_codec_the_browser_cannot_play_is_sent_to_the_fallback_with_its_quality_and_mask_choice(self):
        self.camera("mj", "mjpeg")
        message, code = self.refusal("/api/cameras/mj/live/stream?quality=high")
        self.assertEqual((message["type"], message["codec"], message["fallback"], code), ("unsupported", "mjpeg", "/api/cameras/mj/live?quality=high", live_media.CLOSE_UNSUPPORTED))
        self.assertIsNone(self.stream.joined)

    def test_no_conversion_slot_left_refuses_and_gives_the_view_slot_back(self):
        self.camera("h5", "hevc")
        with patch.object(live_media, "MAX_LIVE_TRANSCODES", 0):
            message, code = self.refusal("/api/cameras/h5/live/stream")
        self.assertEqual((message["type"], code), ("busy", live_media.CLOSE_BUSY))
        self.assertIn("conversion slots", message["detail"])
        self.assert_everything_given_back()

    # --- a stream that does not become playable ------------------------------------------------------------------------------------

    def test_a_stream_that_fails_says_why(self):
        self.stream = ScriptedStream(failed="The camera refused the connection")
        message, code = self.refusal("/api/cameras/cam/live/stream")
        self.assertEqual((message, code), ({"type": "error", "detail": "The camera refused the connection"}, live_media.CLOSE_STREAM_FAILED))
        self.assertEqual(len(self.left), 1)  # The viewer left the stream it joined
        self.assert_everything_given_back()

    def test_an_h265_conversion_that_fails_offers_the_fallback_and_says_it_was_converted(self):
        self.camera("h5", "hevc")
        self.stream = ScriptedStream(failed="The encoder stopped")
        message, code = self.refusal("/api/cameras/h5/live/stream")
        self.assertEqual((message["type"], message["converted"], message["fallback"], code), ("unsupported", True, "/api/cameras/h5/live?quality=low", live_media.CLOSE_UNSUPPORTED))
        self.assertTrue(self.stream.joined["transcode"])
        self.assert_everything_given_back()

    def test_a_stream_in_a_codec_the_browser_did_not_ask_for_is_sent_to_the_fallback(self):
        self.stream = ScriptedStream(mime=HEVC)
        message, code = self.refusal("/api/cameras/cam/live/stream")
        self.assertEqual((message["type"], message["codec"], code), ("unsupported", "unknown", live_media.CLOSE_UNSUPPORTED))
        self.assert_everything_given_back()

    # --- watching ------------------------------------------------------------------------------------------------------------------

    def test_a_viewer_gets_init_then_the_segment_then_the_video_and_its_wait_is_recorded(self):
        self.stream = ScriptedStream(items=[b"FRAG1", b"FRAG2"])
        with self.client.websocket_connect("/api/cameras/cam/live/stream") as websocket:
            init = websocket.receive_json()
            self.assertEqual((websocket.receive_bytes(), websocket.receive_bytes(), websocket.receive_bytes()), (b"INIT", b"FRAG1", b"FRAG2"))
        self.assertEqual({key: init[key] for key in ("type", "mime", "profile", "audio", "listening", "codec", "transcoded", "accelerated")},
                         {"type": "init", "mime": H264, "profile": "main", "audio": False, "listening": False, "codec": "h264", "transcoded": False, "accelerated": False})
        deadline = time.monotonic() + 3
        while not self.app.state.live_hub.starts and time.monotonic() < deadline:
            time.sleep(0.02)
        (start,) = self.app.state.live_hub.starts
        self.assertEqual((start["camera_id"], start["joined_running"], start["spawned_ms"]), ("cam", False, 7))  # Where the time went, for the one who started it
        self.assertIn("first_media_ms", start)
        self.assert_everything_given_back()

    def test_a_viewer_too_slow_to_keep_up_is_told_to_reconnect(self):
        self.stream = ScriptedStream(items=[None], closed_reason="too_slow")
        with self.client.websocket_connect("/api/cameras/cam/live/stream") as websocket:
            websocket.receive_json()
            websocket.receive_bytes()
            received, code = self.closing(websocket)
        self.assertEqual(code, live_media.CLOSE_TOO_SLOW)
        self.assertIn('"too_slow"', received[0])
        self.assert_everything_given_back()

    def test_a_stream_that_ends_says_so(self):
        self.stream = ScriptedStream(items=[b"FRAG1", None])
        with self.client.websocket_connect("/api/cameras/cam/live/stream") as websocket:
            websocket.receive_json()
            websocket.receive_bytes()
            self.assertEqual(websocket.receive_bytes(), b"FRAG1")
            received, code = self.closing(websocket)
        self.assertEqual(code, live_media.CLOSE_STREAM_FAILED)
        self.assertIn("The camera stream ended", received[0])

    def test_access_that_ends_while_watching_closes_the_stream_with_the_reason(self):
        for reason, code, word in (("access", live_media.CLOSE_FORBIDDEN, "access to this camera has ended"), ("credential", live_media.CLOSE_UNAUTHORIZED, "sign-in has ended")):
            with self.subTest(reason=reason):
                self.stream = ScriptedStream(items=[b"FRAG1"])

                async def check(guard, reason=reason):
                    guard.reason = reason
                    return False

                with patch.object(privacy.StreamGuard, "check", check), self.client.websocket_connect("/api/cameras/cam/live/stream") as websocket:
                    websocket.receive_json()
                    websocket.receive_bytes()
                    received, closed = self.closing(websocket)
                self.assertEqual(closed, code)
                self.assertIn(word, received[0])
                self.assertNotIn(b"FRAG1", [item for item in received if isinstance(item, bytes)])  # Nothing more was sent once access ended
                self.assert_everything_given_back()

    def test_privacy_masks_that_change_while_watching_end_the_stream_to_apply_them(self):
        self.stream = ScriptedStream(items=[b"FRAG1"])

        async def check(guard):
            guard.reason = "masks"
            return False

        with patch.object(privacy.StreamGuard, "check", check), self.client.websocket_connect("/api/cameras/cam/live/stream") as websocket:
            websocket.receive_json()
            websocket.receive_bytes()
            received, code = self.closing(websocket)
        self.assertEqual(code, live_media.CLOSE_STREAM_FAILED)
        self.assertIn("privacy masks changed", received[0])

    # --- audio ---------------------------------------------------------------------------------------------------------------------

    def test_listening_is_offered_only_with_an_audio_track_and_claims_and_returns_a_slot(self):
        self.camera("mic", "h264", audio=True)
        self.stream = ScriptedStream(mime=H264_AAC)
        with self.client.websocket_connect("/api/cameras/mic/live/stream?audio=true") as websocket:
            init = websocket.receive_json()
            websocket.receive_bytes()
            self.assertEqual((init["audio"], init["listening"]), (True, True))
            self.assertEqual(live_media._live_audio_count, 1)
            websocket.send_json({"type": "listen", "on": False})
            self.assertEqual(websocket.receive_json(), {"type": "listening", "on": False, "detail": None})
            self.assertEqual(live_media._live_audio_count, 0)
            with patch.object(live_media, "MAX_LIVE_AUDIO_LISTENERS", 0):
                websocket.send_json({"type": "listen", "on": True})
                self.assertEqual(websocket.receive_json(), {"type": "listening", "on": False, "detail": "All live audio slots are in use"})
            websocket.send_json({"type": "listen", "on": True})
            self.assertEqual(websocket.receive_json(), {"type": "listening", "on": True, "detail": None})
        self.assert_everything_given_back()  # The slot it held when the page closed is given back

    def test_a_stream_without_an_audio_track_offers_no_listening(self):
        self.camera("mic", "h264", audio=True)
        self.stream = ScriptedStream(mime=H264)
        with self.client.websocket_connect("/api/cameras/mic/live/stream?audio=true") as websocket:
            init = websocket.receive_json()
            websocket.receive_bytes()
            self.assertEqual((init["audio"], init["listening"]), (False, False))
            websocket.send_json({"type": "listen", "on": True})
            self.assertEqual(websocket.receive_json(), {"type": "listening", "on": False, "detail": "This camera's audio is not enabled"})
        self.assert_everything_given_back()

    def test_a_page_that_closes_before_the_stream_is_ready_gives_everything_back(self):
        self.stream = ScriptedStream(ready=False)
        with self.client.websocket_connect("/api/cameras/cam/live/stream"):
            deadline = time.monotonic() + 3
            while self.stream.joined is None and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertIsNotNone(self.stream.joined)
        self.assert_everything_given_back()
        deadline = time.monotonic() + 3
        while not self.left and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.left), 1)


if __name__ == "__main__":
    unittest.main()
