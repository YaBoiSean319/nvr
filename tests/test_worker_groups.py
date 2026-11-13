"""A worker that is killed must not leave its FFmpeg processes running (#322)."""
from __future__ import annotations

import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH", "GROUP_SPAWNER_CHILD")


def alive(pid: int) -> bool:
    """Running, not merely a zombie waiting to be reaped."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


def wait_until(condition, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class WorkerGroupTests(unittest.TestCase):
    def setUp(self):
        from app.store import initialize
        from app.workers import supervisor

        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        self.child_file = root / "child.pid"
        os.environ["GROUP_SPAWNER_CHILD"] = str(self.child_file)
        initialize()
        self.supervisor_module = supervisor
        self.worker = supervisor.Worker("recorder", "tests.group_spawner")
        self.supervisor = supervisor.Supervisor([self.worker])
        self.child_pid = 0

    def tearDown(self):
        if self.worker.process is not None and self.worker.process.poll() is None:
            try:
                os.killpg(self.worker.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.worker.process.wait()
        if self.child_pid and alive(self.child_pid):
            os.kill(self.child_pid, signal.SIGKILL)
        for key in _ENV:
            os.environ.pop(key, None)
        self.temporary.cleanup()

    def start_worker_with_child(self) -> int:
        self.worker.start()
        self.assertTrue(wait_until(lambda: self.child_file.exists() and self.child_file.read_text(encoding="utf-8").strip()), "the stand-in worker did not start its child")
        self.child_pid = int(self.child_file.read_text(encoding="utf-8"))
        self.assertTrue(alive(self.child_pid))
        return self.child_pid

    def test_a_worker_is_the_leader_of_its_own_process_group(self):
        self.start_worker_with_child()
        self.assertEqual(os.getpgid(self.worker.process.pid), self.worker.process.pid)
        self.assertEqual(os.getpgid(self.child_pid), self.worker.process.pid)
        self.assertNotEqual(os.getpgid(self.worker.process.pid), os.getpgrp())

    def test_a_worker_killed_from_outside_leaves_no_child_behind(self):
        """What the kernel's out-of-memory killer does: SIGKILL to the worker alone, not its children."""
        self.start_worker_with_child()
        os.kill(self.worker.process.pid, signal.SIGKILL)
        with self.assertLogs("nvr.supervisor", level="WARNING") as logged:
            self.assertTrue(wait_until(lambda: (self.supervisor._watch(self.worker), self.worker.process is None)[1]))
        self.assertTrue(wait_until(lambda: not alive(self.child_pid)), "the worker's child is still running")
        self.assertTrue(any("left running" in line for line in logged.output), logged.output)

    def test_a_stalled_worker_is_killed_together_with_its_child(self):
        self.start_worker_with_child()
        with patch.object(self.supervisor_module.Supervisor, "_hung", return_value=True), self.assertLogs("nvr.supervisor", level="WARNING"):
            self.supervisor._watch(self.worker)
        self.assertIsNone(self.worker.process)
        self.assertIn("no heartbeat", self.supervisor._explain(self.worker, -9) or "")
        self.assertTrue(wait_until(lambda: not alive(self.child_pid)), "the stalled worker's child is still running")

    def test_stopping_the_supervisor_ends_a_child_that_outlived_its_worker(self):
        self.start_worker_with_child()
        with self.assertLogs("nvr.supervisor", level="WARNING"):
            self.supervisor._stop_all()
        self.assertTrue(wait_until(lambda: not alive(self.child_pid)), "the worker's child is still running after the stop")

    def test_nothing_is_ended_when_a_worker_left_nothing_behind(self):
        self.worker.start()
        process = self.worker.process
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        self.assertFalse(self.supervisor_module.end_group(process))


if __name__ == "__main__":
    unittest.main()
