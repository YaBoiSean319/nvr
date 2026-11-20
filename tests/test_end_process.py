"""Ending a child process (FFmpeg): ask it to stop, wait a grace period, then kill it, and in every case wait for it (#387, #322).

This was written out eight times (the recorder three times, live video three, the AI frame reader, the export encoder). These tests pin it
on real child processes, through the recorder's copy, before the copies become one: a process that stops when asked is not killed, one
that ignores the request is killed once its grace period is over, and one that has already ended is left alone. A process is always
waited for, so none is left behind as a zombie.
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
import unittest

COOPERATIVE = "import time\ntime.sleep(60)\n"
STUBBORN = "import signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nsys.stdout.write('ready\\n'); sys.stdout.flush()\ntime.sleep(60)\n"


async def child(code: str) -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE)
    if "ready" in code:
        await process.stdout.readline()  # Its SIGTERM handler is in place before it is asked to stop
    return process


def end(process: asyncio.subprocess.Process) -> "asyncio.Future[None]":
    from app.recording import RecordingManager
    return RecordingManager()._end_ffmpeg(process)


class EndProcessTests(unittest.TestCase):
    def test_a_process_that_stops_when_asked_is_not_killed(self):
        async def run():
            process = await child(COOPERATIVE)
            started = time.monotonic()
            await end(process)
            return process.returncode, time.monotonic() - started
        code, took = asyncio.run(run())
        self.assertEqual(code, -signal.SIGTERM)  # Ended by the request to stop, not by a kill
        self.assertLess(took, 2)

    def test_a_process_that_ignores_the_request_is_killed_after_the_grace_period(self):
        async def run():
            process = await child(STUBBORN)
            started = time.monotonic()
            await end(process)
            return process.returncode, time.monotonic() - started
        code, took = asyncio.run(run())
        self.assertEqual(code, -signal.SIGKILL)
        self.assertGreaterEqual(took, 4.5)  # The recorder's grace period is five seconds
        self.assertLess(took, 8)

    def test_a_process_that_has_already_ended_is_left_alone(self):
        async def run():
            process = await child("pass\n")
            await process.wait()
            await end(process)
            await end(None)  # And no process at all
            return process.returncode
        self.assertEqual(asyncio.run(run()), 0)


if __name__ == "__main__":
    unittest.main()
