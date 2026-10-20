from __future__ import annotations

import asyncio
import http.server
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.motion import MotionClipAssembler, parse_live_playlist
from app.onvif_events import CameraMotionEvent, parse_motion_notifications, subscribe_and_pull
from app.server_motion import FrameMotionDetector


class MotionTests(unittest.TestCase):
    def test_server_detector_respects_zones_sensitivity_and_quiet_scene(self):
        zone = {"x":0.25,"y":0.0,"width":0.5,"height":1.0}
        detector = FrameMotionDetector([zone], sensitivity=50, frame_width=20, frame_height=10)
        still = bytes(200)
        self.assertIsNone(detector.process(still))
        self.assertIsNone(detector.process(still))
        outside = bytearray(still)
        for y in range(10):
            for x in range(20):
                if x < 5 or x >= 15:
                    outside[y * 20 + x] = 255
        self.assertIsNone(detector.process(bytes(outside)))
        inside = bytearray(outside)
        for y in range(10):
            for x in range(5, 15):
                inside[y * 20 + x] = 180
        self.assertIsNone(detector.process(bytes(inside)))
        self.assertTrue(detector.process(bytes(outside)))
        self.assertIsNone(detector.process(still))
        self.assertIsNone(detector.process(still))
        self.assertFalse(detector.process(still))
        self.assertLess(FrameMotionDetector(sensitivity=100).threshold, FrameMotionDetector(sensitivity=1).threshold)

    def test_server_detector_rejects_out_of_bounds_zones(self):
        with self.assertRaises(ValueError):
            FrameMotionDetector([{"x":0.9,"y":0,"width":0.2,"height":1}])

    def test_parse_onvif_motion_transition_and_ignore_non_motion(self):
        xml = b'''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" xmlns:tt="http://www.onvif.org/ver10/schema">
          <wsnt:NotificationMessage><wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
            <wsnt:Message><tt:Message UtcTime="2026-10-20T12:00:00Z"><tt:Data><tt:SimpleItem Name="IsMotion" Value="true"/></tt:Data></tt:Message></wsnt:Message>
          </wsnt:NotificationMessage>
          <wsnt:NotificationMessage><wsnt:Topic>tns1:Device/Temperature</wsnt:Topic>
            <wsnt:Message><tt:Message UtcTime="2026-10-20T12:00:00Z"><tt:Data><tt:SimpleItem Name="State" Value="true"/></tt:Data></tt:Message></wsnt:Message>
          </wsnt:NotificationMessage></s:Envelope>'''
        events = parse_motion_notifications(xml)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].active)
        self.assertEqual(events[0].topic, "tns1:RuleEngine/CellMotionDetector/Motion")
        self.assertEqual(events[0].event_time, datetime(2026, 10, 20, 12, tzinfo=timezone.utc))

    def test_onvif_pullpoint_handshake_delivers_camera_motion_event(self):
        seen: list[CameraMotionEvent] = []
        statuses: list[str] = []
        ready = asyncio.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                endpoint = f"http://127.0.0.1:{self.server.server_port}"
                if self.path == "/device":
                    body = f'''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:td="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body><td:GetCapabilitiesResponse><td:Capabilities><tt:Events><tt:XAddr>{endpoint}/events</tt:XAddr></tt:Events></td:Capabilities></td:GetCapabilitiesResponse></s:Body></s:Envelope>'''
                elif self.path == "/events":
                    body = f'''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tev="http://www.onvif.org/ver10/events/wsdl" xmlns:wsa="http://www.w3.org/2005/08/addressing"><s:Body><tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference><wsa:Address>{endpoint}/pull</wsa:Address></tev:SubscriptionReference></tev:CreatePullPointSubscriptionResponse></s:Body></s:Envelope>'''
                else:
                    body = '''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body><tev:PullMessagesResponse xmlns:tev="http://www.onvif.org/ver10/events/wsdl"><wsnt:NotificationMessage><wsnt:Topic>RuleEngine/CellMotionDetector/Motion</wsnt:Topic><wsnt:Message><tt:Message UtcTime="2026-10-20T12:00:00Z"><tt:Data><tt:SimpleItem Name="IsMotion" Value="true"/></tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage></tev:PullMessagesResponse></s:Body></s:Envelope>'''
                payload = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/soap+xml")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        async def run_subscription():
            async def on_event(event):
                seen.append(event)
                ready.set()
            task = asyncio.create_task(subscribe_and_pull(
                f"http://127.0.0.1:{server.server_port}/device", "", "", on_event, statuses.append
            ))
            try:
                await asyncio.wait_for(ready.wait(), timeout=5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        try:
            asyncio.run(run_subscription())
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].active)
        self.assertEqual(seen[0].topic, "RuleEngine/CellMotionDetector/Motion")
        self.assertIn("healthy", statuses)

    def test_motion_clip_is_persisted_as_playable_video_with_source_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
            os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
            os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
            try:
                from app.store import connect, initialize
                initialize()
                camera_id = str(uuid.uuid4())
                with connect() as db:
                    db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (camera_id, "Motion test", b"encrypted"))
                spool = root / "spool"
                spool.mkdir()
                source = spool / "segment-000001.ts"
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=4:size=64x64:rate=10", "-an", "-c:v", "libx264", "-preset", "ultrafast",
                    "-f", "mpegts", str(source),
                ], check=True, timeout=20)
                start = datetime.now(timezone.utc) - timedelta(seconds=2)
                spool.joinpath("live.m3u8").write_text(
                    "#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:" + start.isoformat() + "\n#EXTINF:4.0,\nsegment-000001.ts\n",
                    encoding="utf-8",
                )
                assembler = MotionClipAssembler(camera_id, spool, pre_roll_seconds=0, post_roll_seconds=1)
                source_time = datetime.now(timezone.utc)
                async def finalize():
                    await assembler.ingest(CameraMotionEvent(True, source_time, "RuleEngine/CellMotionDetector/Motion"))
                    await assembler.ingest(CameraMotionEvent(False, source_time + timedelta(seconds=1), "RuleEngine/CellMotionDetector/Motion"))
                    await asyncio.sleep(1.1)
                    await assembler.poll()
                asyncio.run(finalize())
                self.assertIsNone(assembler.capture)
                with connect() as db:
                    event = db.execute("SELECT * FROM motion_events WHERE camera_id=?", (camera_id,)).fetchone()
                    segment = db.execute("SELECT * FROM recording_segments WHERE camera_id=?", (camera_id,)).fetchone()
                self.assertEqual(event["status"], "complete")
                self.assertEqual(event["source_topic"], "RuleEngine/CellMotionDetector/Motion")
                self.assertTrue(event["pre_roll_complete"])
                self.assertIsNotNone(event["actual_start"])
                self.assertIsNotNone(event["actual_end"])
                self.assertEqual(segment["id"], event["segment_id"])
                media = root / "recordings" / segment["relative_path"]
                probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(media)], capture_output=True, text=True, check=True, timeout=10)
                self.assertGreater(float(probe.stdout.strip()), 0)
                from fastapi.testclient import TestClient
                from app.main import app
                from app.security import hash_password
                with connect() as db:
                    db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", ("test-admin", hash_password("StrongPassphrase2026"), "administrator"))
                with TestClient(app) as client:
                    login = client.post("/api/auth/login", json={"username":"test-admin","password":"StrongPassphrase2026"})
                    self.assertEqual(login.status_code, 200, login.text)
                    browser_events = client.get(f"/api/cameras/{camera_id}/motion-events")
                    self.assertEqual(browser_events.status_code, 200, browser_events.text)
                    view = browser_events.json()["events"][0]
                    self.assertEqual(view["status"], "complete")
                    playback = client.get(view["media_url"])
                    self.assertEqual(playback.status_code, 200, playback.text[:200])
                    self.assertEqual(playback.headers["content-type"], "video/mp4")
                    self.assertGreater(len(playback.content), 128)
            finally:
                for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH"):
                    os.environ.pop(key, None)

    def test_server_fallback_detects_motion_and_serves_persisted_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
            os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
            os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
            try:
                from app.store import connect, initialize
                from app.recording import RecordingManager
                initialize()
                camera_id = str(uuid.uuid4())
                with connect() as db:
                    db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,motion_fallback_enabled) VALUES(?,?,?,1)", (camera_id, "Server fallback integration", b"encrypted"))

                spool = root / "spool"
                spool.mkdir()
                source = spool / "segment-000001.ts"
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=6:size=64x64:rate=10", "-an", "-c:v", "libx264", "-preset", "ultrafast",
                    "-f", "mpegts", str(source),
                ], check=True, timeout=20)
                start = datetime.now(timezone.utc) - timedelta(seconds=2)
                spool.joinpath("live.m3u8").write_text(
                    "#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:" + start.isoformat() + "\n#EXTINF:6.0,\nsegment-000001.ts\n",
                    encoding="utf-8",
                )
                assembler = MotionClipAssembler(camera_id, spool, pre_roll_seconds=0, post_roll_seconds=1)

                real_ffmpeg = shutil.which("ffmpeg")
                self.assertIsNotNone(real_ffmpeg)
                fake_ffmpeg = root / "ffmpeg-frames"
                fake_ffmpeg.write_text(
                    f"#!{sys.executable}\n"
                    "import os, sys\n"
                    f"real = {real_ffmpeg!r}\n"
                    "args = sys.argv[1:]\n"
                    "if 'rawvideo' in args:\n"
                    "    sys.stdin.buffer.read()\n"
                    "    size = 160 * 90\n"
                    "    still = bytes(size)\n"
                    "    moving_a = bytes([100]) * size\n"
                    "    moving_b = bytes([200]) * size\n"
                    "    moving_c = bytes([255]) * size\n"
                    "    sys.stdout.buffer.write(still + moving_a + moving_b + moving_c + still + still + still)\n"
                    "    sys.stdout.buffer.flush()\n"
                    "else:\n"
                    "    os.execv(real, [real, *args])\n",
                    encoding="utf-8",
                )
                fake_ffmpeg.chmod(0o755)
                source_state = {"value":"unavailable"}

                async def detect_and_finalize():
                    with patch("app.recording.shutil.which", side_effect=lambda name: str(fake_ffmpeg) if name == "ffmpeg" else shutil.which(name)):
                        with self.assertRaises((asyncio.IncompleteReadError, OSError)):
                            await RecordingManager()._detect_server_motion(
                                camera_id, "rtsp://127.0.0.1/stream", [], 50, source_state, assembler,
                            )
                        await asyncio.sleep(1.1)
                        await assembler.poll()

                asyncio.run(detect_and_finalize())
                with connect() as db:
                    event = db.execute("SELECT * FROM motion_events WHERE camera_id=?", (camera_id,)).fetchone()
                    segment = db.execute("SELECT * FROM recording_segments WHERE camera_id=?", (camera_id,)).fetchone()
                self.assertIsNotNone(event)
                self.assertEqual(event["source"], "server")
                self.assertEqual(event["status"], "complete")
                self.assertIsNotNone(segment)

                from fastapi.testclient import TestClient
                from app.main import app
                from app.security import hash_password
                with connect() as db:
                    db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", ("fallback-admin", hash_password("StrongPassphrase2026"), "administrator"))
                with TestClient(app) as client:
                    login = client.post("/api/auth/login", json={"username":"fallback-admin","password":"StrongPassphrase2026"})
                    self.assertEqual(login.status_code, 200, login.text)
                    events = client.get(f"/api/cameras/{camera_id}/motion-events")
                    self.assertEqual(events.status_code, 200, events.text)
                    view = events.json()["events"][0]
                    self.assertEqual(view["source"], "server")
                    self.assertEqual(view["status"], "complete")
                    playback = client.get(view["media_url"])
                    self.assertEqual(playback.status_code, 200, playback.text[:200])
                    self.assertEqual(playback.headers["content-type"], "video/mp4")
                    self.assertGreater(len(playback.content), 128)
            finally:
                for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH"):
                    os.environ.pop(key, None)

    def test_new_motion_event_does_not_discard_expired_unfinalized_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
            os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
            os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
            try:
                from app.store import connect, initialize
                initialize()
                camera_id = str(uuid.uuid4())
                with connect() as db:
                    db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (camera_id, "Merge race", b"encrypted"))
                assembler = MotionClipAssembler(camera_id, root / "spool", pre_roll_seconds=0, post_roll_seconds=1)
                topic = "RuleEngine/CellMotionDetector/Motion"
                event_time = datetime.now(timezone.utc)
                async def ingest_events():
                    await assembler.ingest(CameraMotionEvent(True, event_time, topic))
                    await assembler.ingest(CameraMotionEvent(False, event_time + timedelta(seconds=1), topic))
                    first_capture = assembler.capture.event_id
                    await asyncio.sleep(1.05)
                    await assembler.ingest(CameraMotionEvent(True, event_time + timedelta(seconds=2), topic))
                    return first_capture
                first_capture = asyncio.run(ingest_events())
                self.assertEqual(assembler.capture.event_id, first_capture)
                self.assertTrue(assembler.capture.active)
                self.assertEqual(len(assembler.capture.source_events), 3)
                with connect() as db:
                    events = db.execute("SELECT id,status FROM motion_events WHERE camera_id=?", (camera_id,)).fetchall()
                self.assertEqual([(row["id"], row["status"]) for row in events], [(first_capture, "recording")])
                asyncio.run(assembler.stop())
            finally:
                for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH"):
                    os.environ.pop(key, None)

    def test_server_fallback_failure_and_camera_event_recovery_stay_motion_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
            os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
            os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
            try:
                from app.store import connect, initialize
                from app.recording import RecordingManager
                initialize()
                camera_id = str(uuid.uuid4())
                with connect() as db:
                    db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_enabled,recording_mode,motion_fallback_enabled,recording_status) VALUES(?,?,?,1,'motion',1,'reconnecting')", (camera_id, "Fallback health", b"encrypted"))
                manager = RecordingManager()
                source_state = {"value":"unavailable"}
                async def observe():
                    changed = asyncio.Event()
                    attempted = asyncio.Event()
                    async def detector_stops(*args):
                        attempted.set()
                        raise OSError("synthetic detector stoppage")
                    manager._detect_server_motion = detector_stops
                    task = asyncio.create_task(manager._watch_server_motion_fallback(camera_id, "rtsp://camera", [], 50, source_state, changed, None))
                    try:
                        await asyncio.wait_for(attempted.wait(), timeout=2)
                        for _ in range(20):
                            with connect() as db:
                                row = db.execute("SELECT recording_status,server_motion_status,recording_mode FROM cameras WHERE id=?", (camera_id,)).fetchone()
                            if row["server_motion_status"] == "unavailable":
                                break
                            await asyncio.sleep(0.05)
                        self.assertEqual(row["recording_status"], "unavailable")
                        self.assertEqual(row["server_motion_status"], "unavailable")
                        self.assertEqual(row["recording_mode"], "motion")
                        source_state["value"] = "healthy"
                        changed.set()
                        with connect() as db:
                            db.execute("UPDATE cameras SET motion_source_status='healthy',recording_status='buffering',last_error=NULL WHERE id=?", (camera_id,))
                        for _ in range(50):
                            with connect() as db:
                                recovered = db.execute("SELECT recording_mode,server_motion_status FROM cameras WHERE id=?", (camera_id,)).fetchone()
                            if recovered["server_motion_status"] == "waiting":
                                break
                            await asyncio.sleep(0.1)
                        self.assertEqual(recovered["recording_mode"], "motion")
                        self.assertEqual(recovered["server_motion_status"], "waiting")
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                asyncio.run(observe())
            finally:
                for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH"):
                    os.environ.pop(key, None)

    def test_live_playlist_rejects_non_local_segment_references(self):
        now = datetime.now(timezone.utc).isoformat()
        playlist = f"#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:{now}\n#EXTINF:2.0,\n../outside.ts\n#EXTINF:2.0,\nsegment-000001.ts\n"
        segments = parse_live_playlist(playlist)
        self.assertEqual([segment.name for segment in segments], ["segment-000001.ts"])


if __name__ == "__main__":
    unittest.main()
