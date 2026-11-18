"""What ``RecordingManager._run_continuous`` does at each exit, including a recording that has gone stale while FFmpeg is still running (#286)."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")


class RunContinuousTests(unittest.TestCase):
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

    def camera(self, *, source: bytes | None = None, audio: int = 0) -> str:
        from app.credentials import encrypt
        from app.store import connect

        camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute(
                "INSERT INTO cameras(id,name,stream_url_ciphertext,recording_enabled,recording_mode,audio_enabled) VALUES(?,?,?,1,'continuous',?)",
                (camera_id, f"Continuous pin {camera_id[:8]}", source if source is not None else encrypt("rtsp://127.0.0.1:9/stream"), audio),
            )
        return camera_id

    def row(self, camera_id: str):
        from app.store import connect

        with connect() as db:
            return db.execute("SELECT recording_status,last_error,stream_fault,recording_fault_since FROM cameras WHERE id=?", (camera_id,)).fetchone()

    def fake_ffmpeg(self, body: str) -> Path:
        script = self.root / "ffmpeg"
        script.write_text(f"#!/usr/bin/env python3\nimport sys, time\n{body}\n", encoding="utf-8")
        script.chmod(0o755)
        return script

    def run_loop(self, camera_id: str, fake: Path | None, *, seconds: float | None = None, fault: str = "camera_offline"):
        """Run the loop to its end, or for ``seconds`` and then stop the manager. Returns whatever the loop raised."""
        from app.recording import RecordingManager

        manager = RecordingManager()

        async def stream_fault(source):
            return fault

        async def body():
            task = asyncio.create_task(manager._run_continuous(camera_id))
            if seconds is not None:
                await asyncio.sleep(seconds)
                manager.stopping = True
            try:
                await asyncio.wait_for(task, timeout=40)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise

        which = (lambda name: str(fake)) if fake else (lambda name: None)
        with patch("app.recording.shutil.which", side_effect=which), patch("app.recording._stream_fault", stream_fault):
            try:
                asyncio.run(body())
            except Exception as exc:  # noqa: BLE001 - what the loop raised is the finding
                return exc
        return None

    def test_a_camera_that_is_off_stops_without_starting_ffmpeg(self):
        from app.store import connect

        camera_id = self.camera()
        with connect() as db:
            db.execute("UPDATE cameras SET recording_enabled=0 WHERE id=?", (camera_id,))
        self.assertIsNone(self.run_loop(camera_id, self.fake_ffmpeg("sys.exit(1)")))
        self.assertEqual(self.row(camera_id)["recording_status"], "stopped")

    def test_a_missing_ffmpeg_marks_the_camera_unavailable(self):
        camera_id = self.camera()
        self.assertIsNone(self.run_loop(camera_id, None))
        row = self.row(camera_id)
        self.assertEqual((row["recording_status"], row["last_error"]), ("unavailable", "FFmpeg is not installed"))

    def test_unreadable_credentials_or_an_unsafe_address_fail_the_camera(self):
        from app.credentials import encrypt

        for source in (b"not-encrypted", encrypt("file:///etc/passwd")):
            camera_id = self.camera(source=source)
            self.assertIsNone(self.run_loop(camera_id, self.fake_ffmpeg("sys.exit(1)")))
            row = self.row(camera_id)
            self.assertEqual((row["recording_status"], row["last_error"], row["stream_fault"]), ("failed", "Camera credentials are unavailable", "stream_error"))

    def test_an_unwritable_volume_says_so_and_keeps_retrying(self):
        camera_id = self.camera()
        with patch("app.storage.camera_recordings_dir", side_effect=OSError("read-only")):
            self.assertIsNone(self.run_loop(camera_id, self.fake_ffmpeg("sys.exit(1)"), seconds=1))
        row = self.row(camera_id)
        self.assertEqual((row["recording_status"], row["last_error"], row["stream_fault"]), ("unavailable", "Assigned recording volume is missing or not writable", "disk_unavailable"))

    def test_an_ffmpeg_that_exits_is_reconnecting_with_the_fault_and_its_arguments(self):
        camera_id = self.camera(audio=0)
        argv = self.root / "argv.txt"
        fake = self.fake_ffmpeg(f"open({str(argv)!r}, 'w').write(chr(10).join(sys.argv[1:]))\nsys.stdin.read()\nsys.exit(1)")
        self.assertIsNone(self.run_loop(camera_id, fake, seconds=3))
        row = self.row(camera_id)
        self.assertEqual((row["recording_status"], row["last_error"], row["stream_fault"]), ("reconnecting", "Camera stream interrupted; retrying", "camera_offline"))
        args = argv.read_text(encoding="utf-8").split("\n")
        for pair in (("-c:v", "copy"), ("-f", "concat"), ("-i", "pipe:0"), ("-map", "0:v:0"), ("-segment_time", "60"), ("-reset_timestamps", "1"),
                     ("-segment_format", "mp4"), ("-segment_list_type", "csv"), ("-strftime", "1")):
            self.assertEqual(args[args.index(pair[0]) + 1], pair[1], pair)
        self.assertTrue(args[-1].endswith("segment-%Y%m%dT%H%M%SZ.mp4"))
        self.assertEqual(args[args.index("-segment_list") + 1], str(Path(args[-1]).parent / "segments.csv"))

    def test_an_ffmpeg_that_cannot_start_reconnects_and_says_why(self):
        camera_id = self.camera()
        self.assertIsNone(self.run_loop(camera_id, self.root / "no-such-ffmpeg", seconds=2, fault="stream_error"))
        row = self.row(camera_id)
        self.assertEqual((row["recording_status"], row["last_error"], row["stream_fault"]), ("reconnecting", "Camera stream could not start; retrying", "stream_error"))

    def test_a_recording_that_has_gone_stale_while_ffmpeg_runs_is_marked_and_the_loop_survives(self):
        """FFmpeg is up but nothing has been finalised for far longer than two segments: the fault start is recorded and recording carries on."""
        from app.store import connect

        camera_id = self.camera()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        with connect() as db:
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,100)",
                       (camera_id, f"{camera_id}/old.mp4", (long_ago - timedelta(seconds=60)).isoformat(), long_ago.isoformat()))
        fake = self.fake_ffmpeg("sys.stdin.read()\ntime.sleep(60)")
        raised = self.run_loop(camera_id, fake, seconds=1)
        self.assertIsNone(raised, f"the recording loop died: {raised!r}")
        row = self.row(camera_id)
        self.assertIsNotNone(row["recording_fault_since"])
        self.assertNotEqual(row["recording_status"], "recording")

    # One error must not end a camera's recording, or leave its FFmpeg running beside a new one (#335).

    def pid_ffmpeg(self) -> Path:
        """An FFmpeg stand-in that records its process id and then runs until it is ended."""
        return self.fake_ffmpeg(f"import os\nopen({str(self.root / 'pids')!r}, 'a').write(str(os.getpid()) + '\\n')\nsys.stdin.read()\ntime.sleep(60)")

    def alive(self) -> list[int]:
        running = []
        for pid in (self.root / "pids").read_text().split():
            try:
                os.kill(int(pid), 0)
                running.append(int(pid))
            except ProcessLookupError:
                pass
        return running

    def started(self) -> int:
        return len((self.root / "pids").read_text().split())

    def run_with_one_failing_watch(self, error: Exception):
        from app.recording import RecordingManager

        camera_id = self.camera()
        real = RecordingManager._watch_continuous_output
        calls = {"count": 0}

        async def watch(manager, *args):
            calls["count"] += 1
            if calls["count"] == 1:
                await asyncio.sleep(0.5)  # FFmpeg is up and has recorded its process id
                raise error
            await real(manager, *args)

        with patch.object(RecordingManager, "_watch_continuous_output", watch):
            raised = self.run_loop(camera_id, self.pid_ffmpeg(), seconds=5)
        return camera_id, raised

    def test_a_database_error_while_recording_ends_that_ffmpeg_and_starts_again(self):
        import sqlite3

        camera_id, raised = self.run_with_one_failing_watch(sqlite3.OperationalError("database is locked"))
        self.assertIsNone(raised, f"the recording loop died: {raised!r}")
        self.assertGreaterEqual(self.started(), 2, "recording was not started again")
        self.assertEqual(self.alive(), [], "an FFmpeg was left running")

    def test_a_file_error_while_recording_does_not_leave_a_second_ffmpeg(self):
        camera_id, raised = self.run_with_one_failing_watch(FileNotFoundError("segment removed while it was being looked at"))
        self.assertIsNone(raised, f"the recording loop died: {raised!r}")
        self.assertGreaterEqual(self.started(), 2)
        self.assertEqual(self.alive(), [], "an FFmpeg was left running")

    def test_an_error_outside_the_recording_pass_restarts_the_camera_task(self):
        import sqlite3
        from app.recording import RecordingManager

        camera_id = self.camera()
        manager = RecordingManager()
        attempts = []

        async def once(camera, mode):
            attempts.append(camera)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")

        real_sleep = asyncio.sleep

        async def body():
            with patch.object(manager, "_run_once", once), patch("app.recording.asyncio.sleep", lambda seconds: real_sleep(0)):
                await asyncio.wait_for(manager._run(camera_id, "continuous"), timeout=5)

        asyncio.run(body())
        self.assertEqual(attempts, [camera_id, camera_id])

    def test_the_reconcile_restarts_a_task_that_ended_with_an_error(self):
        from app.recording import RecordingManager

        camera_id = self.camera()
        manager = RecordingManager()
        started = []

        async def failed():
            raise RuntimeError("ended badly")

        async def run(camera, mode):
            started.append(mode)

        async def body():
            dead = asyncio.create_task(failed())
            await asyncio.gather(dead, return_exceptions=True)
            manager.tasks[camera_id], manager.task_modes[camera_id] = dead, "continuous"
            with patch.object(manager, "_run", run):
                await manager.sync_camera(camera_id)
                await asyncio.gather(manager.tasks[camera_id])
                finished = manager.tasks[camera_id]
                await manager.sync_camera(camera_id)  # One that ended without an error is left alone
                self.assertIs(manager.tasks[camera_id], finished)

        asyncio.run(body())
        self.assertEqual(started, ["continuous"])

    def test_a_file_that_vanishes_while_the_folder_is_listed_is_not_an_error(self):
        from app.recording import newest_is_fresh

        folder = self.root / "spool"
        folder.mkdir()
        (folder / "segment-1.ts").write_bytes(b"x")
        (folder / "segment-2.ts").write_bytes(b"x")
        real = Path.stat

        def stat(path, *args, **kwargs):
            if path.name == "segment-1.ts":
                raise FileNotFoundError(path)  # FFmpeg deleted it after the listing
            return real(path, *args, **kwargs)

        with patch.object(Path, "stat", stat):
            self.assertTrue(newest_is_fresh(folder, "segment-*.ts"))
        self.assertFalse(newest_is_fresh(folder, "nothing-*.ts"))
        self.assertFalse(newest_is_fresh(self.root / "no-such-folder", "segment-*.ts"))


if __name__ == "__main__":
    unittest.main()
