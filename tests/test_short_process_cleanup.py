"""An FFmpeg or ffprobe run for one answer (a frame, a duration, a masked picture) is ended when its caller gives up on it.

Each of these waits for its process with a deadline and kills it when the deadline passes. But a caller can also be cancelled: a camera
stopped while its plate frame is being grabbed, a backfill job stopped mid-chunk, a web request ended by a server stopping. A cancelled
wait does not end the process, and an RTSP grab against a camera that never answers has nothing else to end it. These tests start each
one against a stand-in that never finishes, give up on it, and check that the process is gone.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

NEVER_FINISHES = [sys.executable, "-c", "import time\ntime.sleep(60)\n"]


class GivenUpTests(unittest.TestCase):
    def give_up(self, start, *, cancel: bool = True):
        """Run ``start()`` with every process it starts replaced by one that never finishes; cancel it once the process is running (or let
        its own deadline pass). Returns the processes it started, once the caller has returned or raised."""
        started: list[asyncio.subprocess.Process] = []
        real = asyncio.create_subprocess_exec

        async def stand_in(*args, **kwargs):
            process = await real(*NEVER_FINISHES, **kwargs)
            started.append(process)
            return process

        async def run():
            with patch("asyncio.create_subprocess_exec", stand_in):
                task = asyncio.ensure_future(start())
                for _ in range(500):
                    if started:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(started, "the caller never started its process")
                if cancel:
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return started

        return asyncio.run(asyncio.wait_for(run(), timeout=30))

    def assertEnded(self, processes):
        for process in processes:
            self.assertIsNotNone(process.returncode, "a process was left running after its caller gave up on it")

    def test_a_cancelled_plate_frame_grab_ends_its_ffmpeg(self):
        from app.plates import ffmpeg_snapshot
        self.assertEnded(self.give_up(lambda: ffmpeg_snapshot("rtsp://camera.invalid/stream", timeout=60)))

    def test_a_cancelled_privacy_mask_ends_its_ffmpeg(self):
        from app.privacy import _ffmpeg
        self.assertEnded(self.give_up(lambda: _ffmpeg(["-i", "pipe:0", "-f", "image2pipe", "pipe:1"], stdin=b"picture", timeout=60)))

    def test_a_cancelled_backfill_duration_probe_ends_its_ffprobe(self):
        from app.backfill import _probe_seconds
        with tempfile.TemporaryDirectory() as folder:
            self.assertEnded(self.give_up(lambda: _probe_seconds(Path(folder) / "chunk.mp4")))

    def test_a_backfill_duration_probe_past_its_deadline_ends_its_ffprobe(self):
        from app import backfill
        with tempfile.TemporaryDirectory() as folder, patch.object(backfill, "PROBE_TIMEOUT_SECONDS", 0.2, create=True):
            self.assertEnded(self.give_up(lambda: backfill._probe_seconds(Path(folder) / "chunk.mp4"), cancel=False))


if __name__ == "__main__":
    unittest.main()
