"""The recorder's regular passes wait for the database's write lock without stopping its event loop.

Every second for every camera recording on motion, and every two seconds for every camera recording all the time, the recorder's watch
marked the video fresh (and the second wrote the camera's status), and every five
seconds its schedule pass checked each camera's recording coverage, which writes too. All of it ran on the recorder's event loop: while one
write waited for the lock (retention, an export or a backup holding it), every camera's recording supervision and the worker's heartbeat
waited with it, and three long waits in a row reach the supervisor's 30-second kill, which interrupts every camera's recording. Each test
holds the lock from another connection and watches the loop keep turning, as tests/test_loop_stays_free.py does for the web process."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests import loop_stall
from tests.loop_stall import HOLD_SECONDS

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")


class RecorderLoopFreeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(self.root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(self.root / "credentials.key")
        from app.credentials import encrypt
        from app.store import connect, initialize

        initialize()
        self.camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_enabled,recording_mode,recording_status) VALUES(?,?,?,0,'continuous','stopped')",
                       (self.camera_id, "Loop", encrypt("rtsp://camera.invalid/main")))

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def longest_stall_while_locked(self, work) -> float:
        """Run ``work(manager)`` with a new recorder while another connection holds the write lock (tests/loop_stall.py)."""
        from app.recording import RecordingManager

        return loop_stall.longest_stall_while_locked(lambda: work(RecordingManager()))

    @staticmethod
    def one_pass_then_stop(manager, pause: float):
        """A stand-in for ``asyncio.sleep`` that stops the manager at the pause between its passes. ``app.recording.asyncio`` is the asyncio
        module itself, so every other sleep (the test's own ticker's) is passed to the real one."""
        real = asyncio.sleep

        async def sleep(seconds, *args, **kwargs):
            if seconds == pause:
                manager.stopping = True
                seconds = 0
            return await real(seconds, *args, **kwargs)

        return sleep

    def test_the_watch_over_a_running_recording_waits_for_the_lock_without_stopping_the_loop(self):
        output_dir = self.root / "recordings" / "run"
        output_dir.mkdir(parents=True)

        class Running:
            returncode = None

        async def one_pass(manager):
            # A file written just now: the video is marked fresh, a database write. Written here, not before the measurement starts: a file
            # counts as fresh for 10 s, and late in the full suite the garbage collection before the measurement can take several seconds.
            (output_dir / "segment-0001.mp4").write_bytes(b"being written")
            with patch("app.recording.asyncio.sleep", self.one_pass_then_stop(manager, 2)):
                await manager._watch_continuous_output(self.camera_id, Running(), output_dir / "segments.csv", output_dir, 6)

        longest = self.longest_stall_while_locked(one_pass)
        self.assertLess(longest, HOLD_SECONDS / 2, "the recorder's event loop stopped while its watch waited for the write lock")
        from app.store import connect
        with connect() as db:
            marked = db.execute("SELECT last_video_at FROM cameras WHERE id=?", (self.camera_id,)).fetchone()["last_video_at"]
        self.assertIsNotNone(marked)  # Written once the lock was free

    def test_the_watch_over_a_motion_buffer_waits_for_the_lock_without_stopping_the_loop(self):
        spool = self.root / "spool"
        spool.mkdir()

        class Running:
            returncode = None

        class Assembler:
            async def poll(self):
                pass  # No motion event under way: nothing to save

        async def one_pass(manager):
            (spool / "segment-0001.ts").write_bytes(b"being written")  # A fresh segment, written just now as above
            with patch("app.recording.asyncio.sleep", self.one_pass_then_stop(manager, 1)):
                await manager._watch_motion_buffer(self.camera_id, Running(), spool, Assembler(), "rtsp://camera.invalid/main")

        longest = self.longest_stall_while_locked(one_pass)
        self.assertLess(longest, HOLD_SECONDS / 2, "the recorder's event loop stopped while its motion-buffer watch waited for the write lock")
        from app.store import connect
        with connect() as db:
            marked = db.execute("SELECT last_video_at FROM cameras WHERE id=?", (self.camera_id,)).fetchone()["last_video_at"]
        self.assertIsNotNone(marked)

    def status(self) -> str:
        from app.store import connect
        with connect() as db:
            return db.execute("SELECT recording_status FROM cameras WHERE id=?", (self.camera_id,)).fetchone()["recording_status"]

    def test_a_camera_switched_off_is_marked_stopped_without_stopping_the_loop(self):
        # A state change: rarer than the watches, but they come in a burst when many cameras drop at once.
        from app.store import connect
        with connect() as db:
            db.execute("UPDATE cameras SET recording_status='connecting' WHERE id=?", (self.camera_id,))
        longest = self.longest_stall_while_locked(lambda manager: manager.sync_camera(self.camera_id))
        self.assertLess(longest, HOLD_SECONDS / 2, "the recorder's event loop stopped while a camera's state change waited for the write lock")
        self.assertEqual(self.status(), "stopped")

    def test_a_recording_that_cannot_start_says_so_without_stopping_the_loop(self):
        with patch("app.recording.shutil.which", return_value=None):
            longest = self.longest_stall_while_locked(lambda manager: manager._run_continuous(self.camera_id))
        self.assertLess(longest, HOLD_SECONDS / 2, "the recorder's event loop stopped while a camera's state change waited for the write lock")
        self.assertEqual(self.status(), "unavailable")

    def test_the_schedule_pass_checks_coverage_without_stopping_the_loop(self):
        async def one_pass(manager):
            with patch("app.recording.asyncio.sleep", self.one_pass_then_stop(manager, 5)):
                await manager._schedule_loop()

        longest = self.longest_stall_while_locked(one_pass)
        self.assertLess(longest, HOLD_SECONDS / 2, "the recorder's event loop stopped while its coverage check waited for the write lock")
        from app.store import connect
        with connect() as db:
            checked = db.execute("SELECT COUNT(*) FROM coverage_intervals WHERE camera_id=?", (self.camera_id,)).fetchone()[0]
        self.assertEqual(checked, 1)  # The coverage was still checked and written


if __name__ == "__main__":
    unittest.main()
