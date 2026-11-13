"""Pins what ``RecordingManager._run_motion`` does at each exit (#286), so its steps can be split without changing behaviour."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")
_COLUMNS = "recording_status,motion_source_status,server_motion_status,last_error,stream_fault"


class RunMotionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.environ["NVR_DB_PATH"] = str(self.root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(self.root / "credentials.key")
        from app.store import initialize

        initialize()

    def tearDown(self):
        for key in _ENV:
            os.environ.pop(key, None)
        self.temporary.cleanup()

    def camera(self, *, url: bool = True, fallback: int = 0, service: str | None = "http://127.0.0.1:1/onvif", credentials: bytes | None = None, source: bytes | None = None) -> str:
        from app.credentials import encrypt
        from app.store import connect

        camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute(
                "INSERT INTO cameras(id,name,stream_url_ciphertext,recording_enabled,recording_mode,motion_fallback_enabled,motion_service_url,motion_credentials_ciphertext) VALUES(?,?,?,1,'motion',?,?,?)",
                (camera_id, f"Motion pin {camera_id[:8]}", source if source is not None else encrypt("rtsp://127.0.0.1:9/stream") if url else b"not-encrypted", fallback, service, credentials),
            )
        return camera_id

    def row(self, camera_id: str):
        from app.store import connect

        with connect() as db:
            return db.execute(f"SELECT {_COLUMNS} FROM cameras WHERE id=?", (camera_id,)).fetchone()

    def run_once(self, camera_id: str, manager=None):
        from app.recording import RecordingManager

        manager = manager or RecordingManager()
        return asyncio.run(manager._run_motion(camera_id)), manager

    def fake_ffmpeg(self, body: str) -> Path:
        script = self.root / "ffmpeg"
        script.write_text(f"#!/usr/bin/env python3\nimport sys, time\n{body}\n", encoding="utf-8")
        script.chmod(0o755)
        return script

    def test_a_camera_that_is_off_or_missing_stops_without_retrying(self):
        from app.store import connect

        camera_id = self.camera()
        with connect() as db:
            db.execute("UPDATE cameras SET recording_enabled=0 WHERE id=?", (camera_id,))
        self.assertFalse(self.run_once(camera_id)[0])
        self.assertEqual(self.row(camera_id)["recording_status"], "stopped")
        self.assertFalse(self.run_once(str(uuid.uuid4()))[0])

    def test_no_event_service_and_no_fallback_is_unsupported(self):
        camera_id = self.camera(service=None, fallback=0)
        self.assertFalse(self.run_once(camera_id)[0])
        row = self.row(camera_id)
        self.assertEqual((row["recording_status"], row["motion_source_status"], row["server_motion_status"]), ("unsupported", "unsupported", "disabled"))
        self.assertEqual(row["last_error"], "Configure an ONVIF event service or enable server motion fallback")

    def test_a_missing_ffmpeg_marks_the_camera_unavailable(self):
        for fallback, expected in ((0, "disabled"), (1, "unavailable")):
            camera_id = self.camera(fallback=fallback)
            with patch("app.recording.shutil.which", return_value=None):
                self.assertFalse(self.run_once(camera_id)[0])
            row = self.row(camera_id)
            self.assertEqual((row["recording_status"], row["motion_source_status"], row["server_motion_status"]), ("unavailable", "unavailable", expected))
            self.assertEqual(row["last_error"], "FFmpeg is not installed")

    def test_unreadable_credentials_or_an_unsafe_address_fail_the_camera(self):
        from app.credentials import encrypt

        for camera_id in (self.camera(url=False), self.camera(source=encrypt("file:///etc/passwd")), self.camera(credentials=b"not-encrypted")):
            with patch("app.recording.shutil.which", return_value="/usr/bin/true"):
                self.assertFalse(self.run_once(camera_id)[0])
            row = self.row(camera_id)
            self.assertEqual((row["recording_status"], row["motion_source_status"], row["last_error"]), ("failed", "configuration_error", "Camera credentials are unavailable"))

    def spool(self, camera_id: str) -> Path:
        from app.recording import motion_buffer_path

        return motion_buffer_path() / camera_id

    def run_with_fake(self, camera_id: str, body: str, *, stopping: bool = False, fault: str = "camera_offline"):
        from app.recording import RecordingManager

        fake = self.fake_ffmpeg(body)
        manager = RecordingManager()
        manager.stopping = stopping

        async def hang(*args, **kwargs):
            await asyncio.sleep(3600)

        async def stream_fault(source):
            return fault

        with patch("app.recording.shutil.which", return_value=str(fake)), patch("app.recording.subscribe_and_pull", hang), patch("app.recording._stream_fault", stream_fault):
            return asyncio.run(manager._run_motion(camera_id))

    def test_a_buffer_that_exits_is_reconnecting_with_its_fault_and_a_clean_spool(self):
        camera_id = self.camera()
        argv = self.root / "argv.txt"
        self.assertTrue(self.run_with_fake(camera_id, f"open({str(argv)!r}, 'w').write(chr(10).join(sys.argv[1:]))\nsys.stdin.read()\nsys.exit(1)"))
        row = self.row(camera_id)
        self.assertEqual((row["recording_status"], row["stream_fault"], row["last_error"]), ("reconnecting", "camera_offline", "Camera stream buffer interrupted; retrying"))
        self.assertEqual(row["motion_source_status"], "connecting")
        self.assertEqual(row["server_motion_status"], "disabled")
        self.assertFalse(self.spool(camera_id).exists())
        args = argv.read_text(encoding="utf-8").split("\n")
        self.assertEqual(args[args.index("-hls_list_size") + 1], "50")  # (default pre-roll + post-roll + 60) / 2
        for pair in (("-c:v", "copy"), ("-hls_time", "2"), ("-map", "0:v:0"), ("-f", "concat"), ("-i", "pipe:0")):
            self.assertEqual(args[args.index(pair[0]) + 1], pair[1])
        self.assertEqual(args[-1], str(self.spool(camera_id) / "live.m3u8"))
        self.assertIn(str(self.spool(camera_id) / "segment-%06d.ts"), args)

    def test_stopping_the_manager_ends_the_buffer_and_marks_the_camera_stopped(self):
        camera_id = self.camera()
        self.assertTrue(self.run_with_fake(camera_id, "sys.stdin.read()\ntime.sleep(60)", stopping=True))
        self.assertEqual(self.row(camera_id)["recording_status"], "stopped")
        self.assertFalse(self.spool(camera_id).exists())

    def test_an_ffmpeg_that_cannot_start_reconnects_and_says_why(self):
        from app.recording import RecordingManager

        camera_id = self.camera()

        async def stream_fault(source):
            return "stream_error"

        with patch("app.recording.shutil.which", return_value=str(self.root / "no-such-ffmpeg")), patch("app.recording._stream_fault", stream_fault):
            self.assertTrue(asyncio.run(RecordingManager()._run_motion(camera_id)))
        row = self.row(camera_id)
        self.assertEqual((row["recording_status"], row["motion_source_status"], row["stream_fault"]), ("reconnecting", "unavailable", "stream_error"))
        self.assertEqual(row["last_error"], "Camera motion buffer could not start")
        self.assertFalse(self.spool(camera_id).exists())


if __name__ == "__main__":
    unittest.main()
