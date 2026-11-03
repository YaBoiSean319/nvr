import asyncio
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from app.workers import liveness, process_info
from app.workers.supervisor import Supervisor, Worker


class ProcessInfoTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_resident_memory_is_read_from_the_process_status(self):
        self.write("42/status", "Name:\tpython\nVmRSS:\t  2048 kB\nThreads:\t3\n")
        self.assertEqual(process_info.rss_bytes(42, self.root), 2048 * 1024)
        self.assertIsNone(process_info.rss_bytes(43, self.root))
        self.assertIsNotNone(process_info.rss_bytes(__import__("os").getpid()))

    def test_the_memory_limit_is_read_for_cgroup_v2_and_v1_and_none_means_unlimited(self):
        self.write("memory.max", "max\n")
        self.assertIsNone(process_info.memory_limit_bytes(self.root))
        self.write("memory.max", "4294967296\n")
        self.assertEqual(process_info.memory_limit_bytes(self.root), 4294967296)
        other = Path(tempfile.mkdtemp())
        (other / "memory").mkdir()
        (other / "memory/memory.limit_in_bytes").write_text("9223372036854771712\n")
        self.assertIsNone(process_info.memory_limit_bytes(other))
        (other / "memory/memory.limit_in_bytes").write_text("2147483648\n")
        self.assertEqual(process_info.memory_limit_bytes(other), 2147483648)

    def test_oom_kills_are_counted_from_the_cgroup_events(self):
        self.assertIsNone(process_info.oom_kills(self.root))
        self.write("memory.events", "low 0\nhigh 0\nmax 3\noom 2\noom_kill 2\n")
        self.assertEqual(process_info.oom_kills(self.root), 2)
        self.write("memory/memory.oom_control", "oom_kill_disabled 0\nunder_oom 0\n")  # cgroup v1 has no counter to read
        other = Path(tempfile.mkdtemp())
        (other / "memory").mkdir()
        (other / "memory/memory.oom_control").write_text("oom_kill_disabled 0\nunder_oom 0\noom_kill 5\n")
        self.assertIsNone(process_info.oom_kills(other))


class SupervisorReasonTests(unittest.TestCase):
    def worker(self, code=None):
        worker = Worker("ai", "app.workers.ai")
        worker.process = MagicMock()
        worker.process.pid = 99
        worker.process.poll.return_value = code
        worker.started_at = time.monotonic()
        return worker

    def test_a_sigkill_is_blamed_on_the_kernel_only_when_it_counted_an_out_of_memory_kill(self):
        supervisor = Supervisor([])
        worker = self.worker()
        worker.last_rss = 3_000_000_000
        with patch.object(process_info, "oom_kills", return_value=0), patch.object(process_info, "memory_limit_bytes", return_value=4_000_000_000):
            supervisor._oom_kills_seen = 0
            outside = supervisor._explain(worker, -9)
        self.assertIn("from outside the supervisor", outside)
        self.assertIn("3000 MB", outside)
        self.assertIn("container limit 4000 MB", outside)
        with patch.object(process_info, "oom_kills", return_value=1), patch.object(process_info, "memory_limit_bytes", return_value=None):
            supervisor._oom_kills_seen = 0
            kernel = supervisor._explain(worker, -9)
            again = supervisor._explain(worker, -9)  # The same kill is not counted twice
        self.assertIn("killed for lack of memory", kernel)
        self.assertIn("no container memory limit", kernel)
        self.assertNotIn("killed by the kernel", again)

    def test_a_worker_killed_for_missing_its_heartbeat_says_so(self):
        supervisor = Supervisor([])
        worker = self.worker()
        worker.started_at = time.monotonic() - 3600
        with patch.object(Supervisor, "_hung", return_value=True), patch.object(Supervisor, "_exited") as exited:
            worker.process.wait.return_value = -9
            supervisor._watch(worker)
        worker.process.kill.assert_called_once()
        self.assertIn("no heartbeat", supervisor._explain(worker, -9))
        exited.assert_called_once()

    def test_a_kill_by_the_supervisor_does_not_leave_a_stale_count_to_blame_a_later_exit(self):
        supervisor = Supervisor([])
        supervisor._oom_kills_seen = 0
        worker = self.worker()
        worker.killed_reason = "killed by the supervisor: no heartbeat"
        with patch.object(process_info, "oom_kills", return_value=1):
            self.assertEqual(supervisor._explain(worker, -9), "killed by the supervisor: no heartbeat")
            later = supervisor._explain(self.worker(), -9)  # The counter was already advanced to 1
        self.assertIn("from outside the supervisor", later)

    def test_analysis_is_marked_interrupted_before_the_ai_incident_is_announced(self):
        # Whoever reacts to the incident (a person, a webhook, a test) must find analysis already marked interrupted.
        order: list[str] = []
        supervisor = Supervisor([])
        worker = self.worker()
        with patch("app.workers.supervisor.mark_analysis_interrupted", side_effect=lambda reason: order.append("interrupted")), \
                patch.object(Supervisor, "_incident", side_effect=lambda *args, **kwargs: order.append("incident")), \
                patch("app.workers.supervisor.liveness.record"):
            supervisor._exited(worker, -9, time.monotonic())
        self.assertEqual(order, ["interrupted", "incident"])

    def test_other_exits_are_described(self):
        supervisor = Supervisor([])
        self.assertEqual(supervisor._explain(self.worker(), 1), "exited by itself")
        self.assertEqual(supervisor._explain(self.worker(), -11), "killed by signal 11")

    def test_memory_is_sampled_while_the_worker_runs_and_remembered_for_the_exit_message(self):
        supervisor = Supervisor([])
        worker = self.worker()
        with patch.object(process_info, "rss_bytes", side_effect=[500_000_000, 900_000_000, 700_000_000]):
            supervisor._sample_memory(worker, 100.0)
            supervisor._sample_memory(worker, 101.0)  # Too soon: not read
            supervisor._sample_memory(worker, 110.0)
            supervisor._sample_memory(worker, 120.0)
        self.assertEqual((worker.last_rss, worker.peak_rss), (700_000_000, 900_000_000))


class LoopLagTests(unittest.TestCase):
    def test_a_blocked_event_loop_is_logged_with_how_long_it_was_blocked(self):
        async def scenario():
            task = asyncio.create_task(liveness.watch_loop_lag("ai", warn_after=0.2, tick=0.05))
            await asyncio.sleep(0.1)
            time.sleep(0.5)  # Blocks the loop, as a long synchronous call would
            await asyncio.sleep(0.2)
            task.cancel()
        with self.assertLogs("app.workers.liveness", "WARNING") as logged:
            asyncio.run(scenario())
        self.assertIn("ai worker: the event loop was blocked for", logged.output[0])


if __name__ == "__main__":
    unittest.main()
