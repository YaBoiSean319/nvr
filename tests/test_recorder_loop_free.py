"""The recorder's regular passes wait for the database's write lock without stopping its event loop.

Every two seconds for every recording camera, the recorder's watch marked the video fresh and wrote the camera's status, and every five
seconds its schedule pass checked each camera's recording coverage, which writes too. All of it ran on the recorder's event loop: while one
write waited for the lock (retention, an export or a backup holding it), every camera's recording supervision and the worker's heartbeat
waited with it, and three long waits in a row reach the supervisor's 30-second kill, which interrupts every camera's recording. Each test
holds the lock from another connection and watches the loop keep turning, as tests/test_loop_stays_free.py does for the web process."""
from __future__ import annotations

import asyncio
import gc
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

HOLD_SECONDS = 1.0
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
        """Run ``work(manager)`` while another connection holds the write lock; return the longest the event loop went without turning.

        The garbage collector is run first and kept off while this is measured, as in tests/test_loop_stays_free.py: a collection of what
        other tests left behind pauses the whole process, not the loop alone."""
        from app.recording import RecordingManager
        from app.store import database_path

        gc.collect()
        gc.disable()
        self.addCleanup(gc.enable)
        taken, release = threading.Event(), threading.Event()

        def hold() -> None:
            other = sqlite3.connect(database_path(), timeout=5, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                taken.set()
                release.wait(HOLD_SECONDS)
                other.execute("ROLLBACK")
            finally:
                other.close()

        async def body() -> float:
            manager = RecordingManager()
            longest, running = 0.0, True

            async def tick() -> None:
                nonlocal longest
                last = time.monotonic()
                while running:
                    await asyncio.sleep(0.01)
                    now = time.monotonic()
                    longest, last = max(longest, now - last), now

            ticker = asyncio.create_task(tick())
            holder = threading.Thread(target=hold)
            holder.start()
            self.assertTrue(await asyncio.to_thread(taken.wait, 5))
            try:
                await asyncio.wait_for(work(manager), timeout=20)
            finally:
                release.set()
                await asyncio.to_thread(holder.join)
                running = False
                await ticker
            return longest

        return asyncio.run(body())

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
        (output_dir / "segment-0001.mp4").write_bytes(b"being written")  # A fresh file: the video is marked fresh, a database write

        class Running:
            returncode = None

        async def one_pass(manager):
            with patch("app.recording.asyncio.sleep", self.one_pass_then_stop(manager, 2)):
                await manager._watch_continuous_output(self.camera_id, Running(), output_dir / "segments.csv", output_dir, 6)

        longest = self.longest_stall_while_locked(one_pass)
        self.assertLess(longest, HOLD_SECONDS / 2, "the recorder's event loop stopped while its watch waited for the write lock")
        from app.store import connect
        with connect() as db:
            marked = db.execute("SELECT last_video_at FROM cameras WHERE id=?", (self.camera_id,)).fetchone()["last_video_at"]
        self.assertIsNotNone(marked)  # Written once the lock was free

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
