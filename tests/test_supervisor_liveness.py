"""A database that cannot be written does not get healthy workers killed, or stop the supervisor (#336).

A worker's sign of life to the supervisor is a small file, not a database write. The supervisor's own database writes are for the status page:
when they fail it logs and carries on."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.workers import liveness, supervisor

_ENV = ("NVR_DB_PATH", "NVR_CONTROL_DIR")


def locked(*args, **kwargs):
    raise sqlite3.OperationalError("database is locked")


class LivenessCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_CONTROL_DIR"] = str(Path(self.temporary.name) / "run")
        from app.store import initialize

        initialize()

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def worker(self, pid: int | None = None) -> supervisor.Worker:
        worker = supervisor.Worker("recorder", "app.workers.recorder")
        if pid is not None:
            worker.process = type("Process", (), {"pid": pid, "poll": lambda self: None})()
            worker.started_at = time.monotonic() - 3600
        return worker


class PulseTests(LivenessCase):
    def test_a_pulse_is_fresh_for_its_own_process_and_only_for_a_while(self):
        liveness.pulse("recorder")
        self.assertTrue(liveness.pulse_fresh("recorder", os.getpid()))
        self.assertFalse(liveness.pulse_fresh("recorder", os.getpid() + 1), "another process's pulse")
        self.assertFalse(liveness.pulse_fresh("recorder", None))
        self.assertFalse(liveness.pulse_fresh("recorder", os.getpid(), now=time.time() + liveness.STALE_AFTER.total_seconds() + 1))
        self.assertFalse(liveness.pulse_fresh("exporter", os.getpid()), "a worker that has never pulsed")

    def test_the_heartbeat_pulses_although_the_database_cannot_be_written(self):
        async def body():
            with patch.object(liveness, "beat", locked), patch.object(liveness, "HEARTBEAT_SECONDS", 0.01):
                task = asyncio.create_task(liveness.heartbeat_forever("recorder"))
                await asyncio.sleep(0.2)
                self.assertFalse(task.done(), "the heartbeat ended")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        asyncio.run(body())
        self.assertTrue(liveness.pulse_fresh("recorder", os.getpid()))

    def test_the_heartbeat_still_writes_the_status_row_although_the_pulse_cannot_be_written(self):
        liveness.record("recorder", state="starting", pid=os.getpid(), started=True)

        def unwritable(name):
            raise OSError("read-only file system")

        async def body():
            with patch.object(liveness, "pulse", unwritable), patch.object(liveness, "HEARTBEAT_SECONDS", 0.01), self.assertLogs(liveness.logger, level="WARNING"):
                task = asyncio.create_task(liveness.heartbeat_forever("recorder"))
                await asyncio.sleep(0.2)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        asyncio.run(body())
        self.assertTrue(liveness.report(True)[1]["alive"])  # The recorder's row


class SupervisorTests(LivenessCase):
    def test_a_worker_that_pulses_is_not_hung_whatever_the_database_says(self):
        worker = self.worker(pid=os.getpid())
        liveness.pulse("recorder")
        watching = supervisor.Supervisor([worker])
        with patch.object(supervisor, "connect", locked), patch.object(liveness, "connect", locked):
            self.assertFalse(watching._hung(worker, time.monotonic()))
            self.assertTrue(watching._reporting(worker))

    def test_a_worker_whose_pulse_has_stopped_is_hung(self):
        worker = self.worker(pid=os.getpid())
        liveness.pulse("recorder")
        stale = time.time() - liveness.STALE_AFTER.total_seconds() - 5
        os.utime(liveness.pulse_path("recorder"), (stale, stale))
        self.assertTrue(supervisor.Supervisor([worker])._hung(worker, time.monotonic()))

    def test_a_worker_that_has_only_just_started_is_given_time_to_pulse(self):
        worker = self.worker(pid=os.getpid())
        worker.started_at = time.monotonic()
        self.assertFalse(supervisor.Supervisor([worker])._hung(worker, time.monotonic()))

    def test_starting_a_worker_does_not_fail_when_its_state_cannot_be_recorded(self):
        worker = supervisor.Worker("recorder", "app.workers.recorder")
        liveness.pulse("recorder")  # Left by the process before

        class Started:
            pid = 4242

        with patch.object(supervisor.subprocess, "Popen", lambda *args, **kwargs: Started()), patch.object(liveness, "record", locked), \
                self.assertLogs(supervisor.logger, level="ERROR") as logged:
            worker.start()
        self.assertEqual(worker.process.pid, 4242)
        self.assertIn("carrying on", logged.output[0])
        self.assertFalse(liveness.pulse_path("recorder").exists(), "the previous process's pulse was left for the new one")

    def test_stopping_reaches_every_worker_although_state_cannot_be_recorded(self):
        signalled = []

        class Running:
            def __init__(self, pid):
                self.pid = pid

            def poll(self):
                return None if self.pid not in signalled else 0

            def send_signal(self, signum):
                signalled.append(self.pid)

            def wait(self, timeout=None):
                return 0

        workers = [supervisor.Worker(name, f"app.workers.{name}") for name in ("web", "recorder")]
        for number, worker in enumerate(workers):
            worker.process = Running(9000 + number)
        with patch.object(liveness, "record", locked), patch.object(supervisor, "end_group", lambda process: False), self.assertLogs(supervisor.logger, level="ERROR"):
            supervisor.Supervisor(workers)._stop_all()
        self.assertEqual(signalled, [9000, 9001])

    def test_a_failing_watch_of_one_worker_does_not_end_supervision(self):
        workers = [supervisor.Worker(name, f"app.workers.{name}") for name in ("web", "recorder")]
        watching = supervisor.Supervisor(workers)
        seen = []

        def watch(worker):
            seen.append(worker.name)
            if worker.name == "web":
                raise sqlite3.OperationalError("database is locked")
            watching.stopping = True

        with patch.object(supervisor.Worker, "start", lambda self: None), patch.object(watching, "_watch", watch), patch.object(watching, "_stop_all", lambda: None), \
                patch.object(supervisor, "POLL_SECONDS", 0), self.assertLogs(supervisor.logger, level="ERROR"):
            self.assertEqual(watching.run(), 0)
        self.assertEqual(seen, ["web", "recorder"])

    def test_ended_processes_that_are_not_workers_are_collected_and_a_workers_is_left_to_its_watch(self):
        orphan = subprocess.Popen([sys.executable, "-c", "pass"])
        worker_process = subprocess.Popen([sys.executable, "-c", "pass"])
        for _ in range(200):  # Both have ended, neither has been collected
            if all(os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None for process in (orphan, worker_process)):
                break
            time.sleep(0.02)
        worker = self.worker()
        worker.process = worker_process
        watching = supervisor.Supervisor([worker])
        watching._reap_orphans()
        self.assertEqual(worker_process.poll(), 0, "the worker's own exit was taken from its watch")  # Its watch collects it, as on every pass
        watching._reap_orphans()
        with self.assertRaises(ChildProcessError):
            os.waitpid(orphan.pid, os.WNOHANG)  # Already collected
        orphan.returncode = 0  # Collected above: nothing left for Popen to wait for


if __name__ == "__main__":
    unittest.main()
