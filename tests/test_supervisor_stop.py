"""The supervisor stops every worker within Docker's grace period, even when some ignore SIGTERM, and the recorder still has time to finish (#410).

Docker gives the container 30 s (Compose's stop_grace_period) and then kills everything. The supervisor used to stop the workers one after
another and give each the whole grace period, so two slow workers were enough to have the recorder killed before it closed its segments."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_worker_groups import alive, wait_until

BUDGET_SECONDS = 4.0
RECORDER_FINISH_SECONDS = 1.5


class SupervisorStopTests(unittest.TestCase):
    def test_workers_that_ignore_sigterm_are_killed_in_time_and_the_recorder_still_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log, pids = root / "events.log", root / "pids.txt"
            environment = {**os.environ, "NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_RECORDINGS_PATH": str(root / "recordings"),
                           "NVR_CREDENTIAL_KEY_PATH": str(root / "credentials.key"), "NVR_CONTROL_DIR": str(root / "run"),
                           "NVR_STOP_GRACE_SECONDS": str(BUDGET_SECONDS), "STOP_STANDIN_LOG": str(log), "STOP_STANDIN_PIDS": str(pids),
                           "STOP_STANDIN_FINISH_SECONDS": str(RECORDER_FINISH_SECONDS)}
            output = root / "supervisor.log"
            with output.open("w", encoding="utf-8") as stream:
                process = subprocess.Popen([sys.executable, "-m", "tests.stop_supervisor_standin"], env=environment, stdout=stream, stderr=subprocess.STDOUT)
            try:
                self.assertTrue(wait_until(lambda: log.exists() and log.read_text(encoding="utf-8").count(" ready") == 4, 30), "the stand-in workers did not start")
                started = time.monotonic()
                process.send_signal(signal.SIGTERM)  # What Docker does on a stop or a redeploy
                code = process.wait(timeout=60)
                elapsed = time.monotonic() - started
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            workers = dict(line.split() for line in pids.read_text(encoding="utf-8").splitlines())
            events = log.read_text(encoding="utf-8").splitlines()
            logged = output.read_text(encoding="utf-8")
            for name in ("exporter", "ai"):
                if alive(int(workers[name])):
                    os.kill(int(workers[name]), signal.SIGKILL)
        self.assertEqual(code, 0, logged)
        # Within the whole budget, with room to spare under Docker's limit: before #410 this took a budget per stubborn worker and then the recorder's time.
        self.assertLess(elapsed, BUDGET_SECONDS + 1.0, logged)
        recorder = workers["recorder"]
        self.assertIn(f"{recorder} asked", events)
        self.assertIn(f"{recorder} finished", events, "the recorder was killed before it finished")
        for name in ("exporter", "ai"):
            self.assertFalse(alive(int(workers[name])), f"the {name} worker that ignored SIGTERM is still running")
            self.assertIn(f"{name} worker did not stop within", logged)
        self.assertIn("recorder worker stopped after", logged)  # Each worker's stop time is logged, so a slow stop can be traced
        self.assertIn("web worker stopped after", logged)
        self.assertIn("Every worker stopped", logged)


if __name__ == "__main__":
    unittest.main()
