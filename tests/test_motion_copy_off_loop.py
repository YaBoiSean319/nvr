"""Copying a motion event's buffered parts never stops the recorder's event loop.

When a motion event starts, the parts of the buffer it covers (its pre-roll, up to two minutes) are copied into the event's folder, then
each new part as it is written; the folder is deleted once the clip is kept. Measured on a quiet disk this takes milliseconds, but the
recorder's disk is busy writing every camera's stream, and when it falls behind the kernel makes new writes wait. On the recorder's event
loop, that wait stopped every camera's recording supervision and the worker's heartbeat with it. Each copy here is made slow, as on such a
disk, and the loop is watched to keep turning."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SLOW_COPY_SECONDS = 0.3
_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")


def longest_stall(work) -> float:
    """Run ``work()`` on a fresh event loop; return the longest the loop went without turning."""
    async def body() -> float:
        longest, running, last = 0.0, True, time.monotonic()

        async def tick() -> None:
            nonlocal longest, last
            while running:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                longest, last = max(longest, now - last), now

        ticker = asyncio.create_task(tick())
        await asyncio.sleep(0.02)
        try:
            await asyncio.wait_for(work(), timeout=30)
            longest = max(longest, time.monotonic() - last)  # A stall that ends the work is counted too
        finally:
            running = False
            await ticker
        return longest

    return asyncio.run(body())


class MotionCopyOffLoopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        from app.store import connect, initialize

        initialize()
        self.camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (self.camera_id, "Motion copy", b"encrypted"))
        # A buffer of three two-second parts, the newest ending now: a pre-roll of ten seconds covers them all.
        self.spool = root / "spool"
        self.spool.mkdir()
        start = datetime.now(timezone.utc) - timedelta(seconds=6)
        lines = ["#EXTM3U", "#EXT-X-PROGRAM-DATE-TIME:" + start.isoformat()]
        for number in range(3):
            (self.spool / f"segment-{number:06d}.ts").write_bytes(b"part" * 1000)
            lines += ["#EXTINF:2.0,", f"segment-{number:06d}.ts"]
        (self.spool / "live.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def slow_disk(self):
        real = shutil.copyfile

        def copy(source, destination, *args, **kwargs):
            time.sleep(SLOW_COPY_SECONDS)  # A write the kernel makes wait while the disk catches up
            return real(source, destination, *args, **kwargs)

        return patch("app.motion.shutil.copyfile", copy)

    def started(self):
        from app.motion import MotionClipAssembler
        from app.onvif_events import CameraMotionEvent

        assembler = MotionClipAssembler(self.camera_id, self.spool, pre_roll_seconds=10, post_roll_seconds=30)
        return assembler, CameraMotionEvent(True, datetime.now(timezone.utc), "RuleEngine/CellMotionDetector/Motion")

    def test_copying_the_pre_roll_does_not_stop_the_loop(self):
        assembler, motion = self.started()

        async def poll():
            await assembler.ingest(motion)
            await assembler.poll()

        with self.slow_disk():
            longest = longest_stall(poll)
        self.assertLess(longest, SLOW_COPY_SECONDS / 2, "the recorder's event loop stopped while a motion event's parts were copied")
        self.assertEqual(len(assembler.capture.copied), 3)  # All three parts were still copied
        self.assertEqual(len(list(assembler.capture.staging_dir.glob("part-*.ts"))), 3)

    def test_stopping_a_capture_copies_what_is_left_without_stopping_the_loop(self):
        assembler, motion = self.started()

        async def stop():
            await assembler.ingest(motion)
            await assembler.stop()  # The parts are copied, joined (these are not real video, so the clip fails) and the folder removed

        with self.slow_disk():
            longest = longest_stall(stop)
        self.assertLess(longest, SLOW_COPY_SECONDS / 2, "the recorder's event loop stopped while a stopped capture's parts were copied")
        self.assertEqual([path.name for path in self.spool.iterdir() if path.name.startswith("capture-")], [])  # Its folder was removed


if __name__ == "__main__":
    unittest.main()
