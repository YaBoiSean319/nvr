from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import live_media
from app.application import create_app
from app.credentials import encrypt
from app.live_hub import Fragmenter, codec_string
from app.principal import COOKIE_NAME
from app.store import connect
from tests.test_support import TEST_PASSWORD

RTSP = os.environ.get("NVR_RTSP_TEST_URL")
FFMPEG = shutil.which("ffmpeg")


@unittest.skipUnless(FFMPEG, "FFmpeg is required")
class FragmenterTests(unittest.TestCase):
    def test_splits_fragmented_mp4_into_init_and_keyframe_marked_fragments(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "live.mp4"
            subprocess.run([
                FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x240:r=10", "-t", "3",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10", "-an", "-f", "mp4",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "300000", str(output),
            ], check=True, timeout=60)
            data = output.read_bytes()
        fragmenter = Fragmenter()
        fragments = []
        for offset in range(0, len(data), 1000):  # arbitrary chunk boundaries
            fragments += fragmenter.feed(data[offset:offset + 1000])
        self.assertIsNotNone(fragmenter.init)
        self.assertRegex(codec_string(fragmenter.init), r'^video/mp4; codecs="avc1\.64[0-9a-f]{4}"$')
        self.assertTrue(fragments[0].keyframe)
        self.assertEqual(sum(fragment.keyframe for fragment in fragments), 3)
        self.assertGreater(len(fragments), 3)
        # Everything but the trailing random-access index a file (not a pipe) gets.
        self.assertTrue(data.startswith(b"".join([fragmenter.init, *(fragment.data for fragment in fragments)])))


@unittest.skipUnless(FFMPEG and RTSP, "A simulated RTSP publisher is required")
class LiveStreamTests(unittest.TestCase):
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
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,video_codec) VALUES('cam','Lobby',?,'motion','h264')", (encrypt(RTSP),))

    def tearDown(self):
        self.context.__exit__(None, None, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def _receive_stream(self, websocket, fragments: int) -> tuple[dict, bytes, list[bytes]]:
        init = websocket.receive_json()
        self.assertEqual(init["type"], "init", init)
        segment = websocket.receive_bytes()
        return init, segment, [websocket.receive_bytes() for _ in range(fragments)]

    def _refusal(self, path: str, client: TestClient | None = None, **kwargs) -> tuple[dict, int]:
        with (client or self.client).websocket_connect(path, **kwargs) as websocket:
            message = websocket.receive_json()
            with self.assertRaises(WebSocketDisconnect) as closed:
                websocket.receive_json()
        return message, closed.exception.code

    def test_authenticated_viewer_receives_decodable_fragmented_mp4(self):
        with self.client.websocket_connect("/api/cameras/cam/live/stream") as websocket:
            init, segment, fragments = self._receive_stream(websocket, 6)
            status = self.client.get("/api/cameras/cam/live/status").json()
            self.assertEqual((status["state"], status["active_viewers"]), ("live", 1))
        self.assertRegex(init["mime"], r'^video/mp4; codecs="avc1\.')
        self.assertEqual((init["profile"], init["audio"]), ("main", False))
        self.assertTrue(Fragmenter().feed(segment) == [])  # just ftyp+moov
        media = Path(self.temporary.name) / "received.mp4"
        media.write_bytes(segment + b"".join(fragments))
        probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                                "stream=codec_name,nb_read_frames", "-of", "json", str(media)], capture_output=True, text=True, check=True)
        stream = json.loads(probe.stdout)["streams"][0]
        self.assertEqual(stream["codec_name"], "h264")
        self.assertGreater(int(stream["nb_read_frames"]), 0)

    def test_refusals_explain_themselves(self):
        anonymous = TestClient(self.app)
        message, code = self._refusal("/api/cameras/cam/live/stream", anonymous)
        self.assertEqual((message["type"], code), ("unauthorized", 4401))
        self.assertEqual(self._refusal("/api/cameras/missing/live/stream"), ({"type": "not_found", "detail": "Camera not found"}, 4404))
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,video_codec) VALUES('hevc','Yard',?,'motion','hevc')", (encrypt(RTSP),))
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,archived_at) VALUES('old','Old',?,'motion','2026-01-01T00:00:00Z')", (encrypt(RTSP),))
        message, code = self._refusal("/api/cameras/hevc/live/stream?quality=high")
        self.assertEqual((message["type"], message["fallback"], code), ("unsupported", "/api/cameras/hevc/live?quality=high", 4415))
        self.assertEqual(self._refusal("/api/cameras/old/live/stream")[1], 4410)
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/api/cameras/cam/live/stream", headers={"origin": "https://attacker.example"}) as websocket:
                websocket.receive_json()

    def test_viewers_share_one_camera_connection_and_late_viewers_start_at_a_keyframe(self):
        hub = self.app.state.live_hub
        with patch("app.live_hub.LINGER_SECONDS", 0.3):
            with self.client.websocket_connect("/api/cameras/cam/live/stream") as first:
                self._receive_stream(first, 3)
                with self.client.websocket_connect("/api/cameras/cam/live/stream") as second:
                    _, segment, fragments = self._receive_stream(second, 1)
                    fragmenter = Fragmenter()
                    parsed = fragmenter.feed(segment + fragments[0])
                    self.assertTrue(parsed and parsed[0].keyframe, "late viewer did not start at a keyframe")
                    snapshot = hub.snapshot()
                    self.assertEqual(len(snapshot), 1)
                    self.assertEqual((snapshot[0]["viewers"], snapshot[0]["camera_connections"]), (2, 1))
                    pids = [int(pid) for pid in subprocess.run(["pgrep", "-f", "frag_keyframe"], capture_output=True, text=True).stdout.split()]
                    for pid in pids:
                        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
                        self.assertNotIn(RTSP.encode(), command)
                        self.assertIn(b"pipe:0", command)
            deadline = time.monotonic() + 5
            while hub.snapshot() and time.monotonic() < deadline:
                time.sleep(0.1)
        self.assertEqual(hub.snapshot(), [])
        self.assertEqual(live_media._live_view_count, 0)

    def test_a_changed_stream_url_never_joins_the_old_stream(self):
        hub = self.app.state.live_hub
        with self.client.websocket_connect("/api/cameras/cam/live/stream") as first:
            self._receive_stream(first, 1)
            with connect() as db:
                db.execute("UPDATE cameras SET stream_url_ciphertext=? WHERE id='cam'", (encrypt(RTSP + "?rotated=1"),))
            with self.client.websocket_connect("/api/cameras/cam/live/stream") as second:
                second.receive_json()
                streams = {tuple(entry["key"]) for entry in hub.snapshot()}
        self.assertEqual(len(streams), 2)

    def test_slots_are_enforced_and_released_on_disconnect(self):
        with patch.object(live_media, "MAX_LIVE_VIEWERS", 1):
            with self.client.websocket_connect("/api/cameras/cam/live/stream") as first:
                self._receive_stream(first, 1)
                message, code = self._refusal("/api/cameras/cam/live/stream")
                self.assertEqual((message["type"], code), ("busy", 4429))
            deadline = time.monotonic() + 3
            while live_media._live_view_count and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(live_media._live_view_count, 0)
            with self.client.websocket_connect("/api/cameras/cam/live/stream") as again:
                self.assertEqual(again.receive_json()["type"], "init")

    def test_audio_is_offered_only_when_the_stream_has_a_track(self):
        with connect() as db:
            db.execute("UPDATE cameras SET audio_enabled=1,audio_supported=1 WHERE id='cam'")
        with self.client.websocket_connect("/api/cameras/cam/live/stream") as websocket:
            init = websocket.receive_json()
        # The simulated camera publishes video only, so no audio track is offered.
        self.assertFalse(init["audio"])
        self.assertNotIn("mp4a", init["mime"])

    @unittest.skipUnless(os.environ.get("NVR_RTSP_AUDIO_TEST_URL"), "A simulated RTSP publisher with audio is required")
    def test_listening_claims_and_returns_an_audio_slot(self):
        with connect() as db:
            db.execute("UPDATE cameras SET audio_enabled=1,audio_supported=1,stream_url_ciphertext=? WHERE id='cam'", (encrypt(os.environ["NVR_RTSP_AUDIO_TEST_URL"]),))
        with patch.object(live_media, "MAX_LIVE_AUDIO_LISTENERS", 1):
            with self.client.websocket_connect("/api/cameras/cam/live/stream") as first, \
                 self.client.websocket_connect("/api/cameras/cam/live/stream") as second:
                self.assertTrue(first.receive_json()["audio"])
                second.receive_json()
                # One at a time: which of two simultaneous requests the server handles first is not up to the test.
                replies = []
                for websocket in (first, second):
                    websocket.send_json({"type": "listen", "on": True})
                    while True:
                        message = websocket.receive()
                        if message.get("text"):
                            replies.append(json.loads(message["text"]))
                            break
                self.assertEqual([reply["on"] for reply in replies], [True, False])
                self.assertEqual(replies[1]["detail"], "All live audio slots are in use")
                first.send_json({"type": "listen", "on": False})
            deadline = time.monotonic() + 3
            while live_media._live_audio_count and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(live_media._live_audio_count, 0)

    def test_api_tokens_need_the_media_scope(self):
        media = self.client.post("/api/admin/api-tokens", json={"name": "wall", "scopes": ["media"]}).json()["secret"]
        read = self.client.post("/api/admin/api-tokens", json={"name": "status", "scopes": ["read"]}).json()["secret"]
        anonymous = TestClient(self.app)
        message, code = self._refusal("/api/cameras/cam/live/stream", anonymous, headers={"authorization": f"Bearer {read}"})
        self.assertEqual((message["type"], code), ("forbidden", 4403))
        with anonymous.websocket_connect("/api/cameras/cam/live/stream", headers={"authorization": f"Bearer {media}"}) as websocket:
            self.assertEqual(websocket.receive_json()["type"], "init")
        self.assertIsNotNone(self.client.cookies.get(COOKIE_NAME))


if __name__ == "__main__":
    unittest.main()
