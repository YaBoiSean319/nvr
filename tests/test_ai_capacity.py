"""The number of cameras the AI worker analyses follows the hardware (#238): the controller over synthetic measurements, and the sampler over fake cgroup and proc files."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import ai_capacity as cap
from app.ai_capacity import Controller, Sample

GIB = 1 << 30
QUIET = Sample(cpu=0.3, inference_busy=0.2, memory_used=0.3, memory_free=10 * GIB, late_share=0.0)


def grow_until(controller: Controller, start: float, demand: int, minutes: float, sample: Sample = QUIET) -> float:
    """Evaluate every interval for this long with every camera allowed running; returns the time reached."""
    now = start
    while now < start + minutes * 60:
        now += cap.EVALUATE_SECONDS
        controller.evaluate(sample, now, analysed=min(controller.limit, demand), demand=demand)
    return now


class ControllerTests(unittest.TestCase):
    def test_a_new_worker_starts_with_a_few_cameras_and_resumes_a_little_below_where_it_was(self):
        self.assertEqual(Controller.starting(64).limit, cap.START_CAMERAS)
        self.assertEqual(Controller.starting(64, last_limit=40).limit, 30)  # 75%: in case the last one was too many
        self.assertEqual(Controller.starting(8, last_limit=40).limit, 8)  # Never past the ceiling

    def test_it_adds_one_camera_at_a_time_and_only_after_sustained_headroom(self):
        controller = Controller.starting(64)
        for index in range(cap.GROW_AFTER - 1):
            decision = controller.evaluate(QUIET, 15.0 * (index + 1), analysed=4, demand=34)
        self.assertEqual((decision.limit, decision.action), (4, "hold"))
        decision = controller.evaluate(QUIET, 15.0 * cap.GROW_AFTER, analysed=4, demand=34)
        self.assertEqual((decision.limit, decision.action), (5, "grow"))
        decision = controller.evaluate(QUIET, 15.0 * (cap.GROW_AFTER + 1), analysed=5, demand=34)
        self.assertEqual(decision.limit, 5)  # The count starts again after each one

    def test_with_steady_headroom_it_reaches_every_camera_wanted_and_stops_there(self):
        controller = Controller.starting(64)
        grow_until(controller, 0, demand=12, minutes=60)
        self.assertEqual(controller.limit, 12)

    def test_it_does_not_grow_with_nobody_waiting_or_while_cameras_are_still_starting(self):
        controller = Controller.starting(64)
        grow_until(controller, 0, demand=4, minutes=10)
        self.assertEqual(controller.limit, 4)
        controller = Controller.starting(64)
        now = 0.0
        for _ in range(20):
            now += 15
            controller.evaluate(QUIET, now, analysed=2, demand=34)  # Only 2 of the 4 allowed are running yet
        self.assertEqual(controller.limit, 4)

    def test_it_takes_cameras_away_at_once_when_the_cpu_passes_its_limit_and_more_when_it_is_severe(self):
        controller = Controller(ceiling=64, limit=20)
        decision = controller.evaluate(Sample(cpu=0.9, inference_busy=0.3, memory_used=0.3, memory_free=10 * GIB), 100.0, analysed=20, demand=34)
        self.assertEqual((decision.action, decision.limit, decision.factor), ("shed", 18, "cpu"))
        severe = Controller(ceiling=64, limit=20)
        self.assertEqual(severe.evaluate(Sample(cpu=0.98, inference_busy=0.3, memory_used=0.3, memory_free=10 * GIB), 100.0, analysed=20, demand=34).limit, 16)

    def test_each_limit_alone_is_enough_to_shed_and_names_itself(self):
        for sample, factor in (
            (Sample(cpu=0.3, inference_busy=0.95, memory_used=0.3, memory_free=10 * GIB), "inference"),
            (Sample(cpu=0.3, inference_busy=0.3, memory_used=0.9, memory_free=10 * GIB), "memory"),
            (Sample(cpu=0.3, inference_busy=0.3, memory_used=0.5, memory_free=GIB // 2), "memory"),
            (Sample(cpu=0.3, inference_busy=0.3, memory_used=0.3, memory_free=10 * GIB, late_share=0.5), "late"),
        ):
            decision = Controller(ceiling=64, limit=20).evaluate(sample, 100.0, analysed=20, demand=34)
            self.assertEqual((decision.action, decision.limit < 20, decision.factor), ("shed", True, factor), factor)

    def test_between_the_grow_and_shed_thresholds_it_neither_grows_nor_sheds(self):
        controller = Controller(ceiling=64, limit=10)
        busy = Sample(cpu=0.75, inference_busy=0.5, memory_used=0.5, memory_free=10 * GIB)
        now = grow_until(controller, 0, demand=34, minutes=30, sample=busy)
        self.assertEqual(controller.limit, 10)
        self.assertGreater(now, 0)

    def test_after_taking_cameras_away_it_waits_before_taking_more_and_before_growing_back(self):
        controller = Controller(ceiling=64, limit=20)
        over = Sample(cpu=0.9, inference_busy=0.3, memory_used=0.3, memory_free=10 * GIB)
        controller.evaluate(over, 100.0, analysed=20, demand=34)
        again = controller.evaluate(over, 115.0, analysed=18, demand=34)
        self.assertEqual((again.action, again.limit), ("hold", 18))  # Still settling: the first change has not shown yet
        later = controller.evaluate(over, 140.0, analysed=18, demand=34)
        self.assertEqual((later.action, later.limit < 18), ("shed", True))
        controller = Controller(ceiling=64, limit=20)
        controller.evaluate(over, 100.0, analysed=20, demand=34)
        grow_until(controller, 100.0, demand=34, minutes=9)
        self.assertEqual(controller.limit, 18)  # Not before the hold-off

    def test_a_count_that_failed_is_not_tried_again_for_an_hour_but_lower_counts_are(self):
        controller = Controller(ceiling=64, limit=20)
        over = Sample(cpu=0.9, inference_busy=0.3, memory_used=0.3, memory_free=10 * GIB)
        controller.evaluate(over, 100.0, analysed=20, demand=34)
        now = grow_until(controller, 100.0, demand=34, minutes=20)
        self.assertEqual(controller.limit, 19)  # Back up to just under the count that failed, and no further
        self.assertLess(now, 100.0 + cap.AVOID_SECONDS)
        grow_until(controller, now, demand=34, minutes=60)
        self.assertGreater(controller.limit, 19)  # An hour later it may try again

    def test_it_never_goes_below_one_camera_or_above_the_ceiling(self):
        controller = Controller(ceiling=64, limit=1)
        decision = controller.evaluate(Sample(cpu=0.99, inference_busy=0.99, memory_used=0.99, memory_free=GIB // 4), 100.0, analysed=1, demand=5)
        self.assertEqual(decision.limit, 1)
        capped = Controller(ceiling=6, limit=6)
        grow_until(capped, 0, demand=40, minutes=30)
        self.assertEqual(capped.limit, 6)

    def test_the_ceiling_follows_the_cores_and_the_memory(self):
        self.assertEqual(cap.hardware_ceiling(8, 16 * GIB), 49)  # 60% of 16 GB at 200 MB a camera is the tighter bound (64 by cores)
        self.assertEqual(cap.hardware_ceiling(2, 64 * GIB), 16)
        self.assertEqual(cap.hardware_ceiling(8, None), 64)
        self.assertEqual(cap.hardware_ceiling(1, 256 << 20), 1)


class SamplerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cgroup, self.proc = Path(self.directory.name) / "cgroup", Path(self.directory.name) / "proc"
        self.cgroup.mkdir()
        self.proc.mkdir()
        self.now = 1000.0
        self.sampler = cap.Sampler(proc=self.proc, cgroup=self.cgroup, clock=lambda: self.now)

    def write(self, base: Path, name: str, text: str) -> None:
        (base / name).write_text(text)

    def test_cpu_is_the_share_of_the_cores_the_container_may_use(self):
        self.write(self.cgroup, "cpu.max", "400000 100000")  # 4 cores
        self.write(self.cgroup, "cpu.stat", "usage_usec 10000000\nuser_usec 1\n")
        self.assertIsNone(self.sampler.cpu())  # The first reading only sets the mark
        self.now += 10.0
        self.write(self.cgroup, "cpu.stat", "usage_usec 30000000\n")  # 20 CPU-seconds in 10 s on 4 cores
        self.assertAlmostEqual(self.sampler.cpu(), 0.5)
        self.assertEqual(self.sampler.cores(), 4.0)

    def test_without_a_cgroup_the_machines_cpu_is_used(self):
        self.sampler.cores = lambda: 2.0  # type: ignore[method-assign]
        self.write(self.proc, "stat", "cpu  100 0 100 800 0 0 0 0\n")
        self.assertIsNone(self.sampler.cpu())
        self.now += 10.0
        busy_ticks = 400  # Of 1200 ticks: a third of the machine
        self.write(self.proc, "stat", f"cpu  {100 + busy_ticks // 2} 0 {100 + busy_ticks // 2} {800 + 800} 0 0 0 0\n")
        import os
        ticks = os.sysconf("SC_CLK_TCK")
        self.assertAlmostEqual(self.sampler.cpu(), min(1.0, (busy_ticks / ticks) / (10.0 * 2.0)))

    def test_memory_is_what_the_container_uses_without_its_file_cache(self):
        self.write(self.cgroup, "memory.max", str(16 * GIB))
        self.write(self.cgroup, "memory.current", str(8 * GIB))
        self.write(self.cgroup, "memory.stat", f"anon 1\ninactive_file {2 * GIB}\n")
        used, free, total = self.sampler.memory()
        self.assertAlmostEqual(used, 6 / 16)
        self.assertEqual((free, total), (10 * GIB, 16 * GIB))

    def test_with_no_limit_the_machines_memory_is_used(self):
        self.write(self.cgroup, "memory.max", "max")
        self.write(self.proc, "meminfo", f"MemTotal:       {16 * GIB // 1024} kB\nMemFree: 1 kB\nMemAvailable:   {4 * GIB // 1024} kB\n")
        used, free, total = self.sampler.memory()
        self.assertAlmostEqual(used, 0.75)
        self.assertEqual((free, total), (4 * GIB, 16 * GIB))

    def test_nothing_readable_gives_nothing_not_a_guess(self):
        self.assertEqual(self.sampler.memory(), (None, None, None))
        self.assertIsNone(self.sampler.cpu())


class CapacityTests(unittest.TestCase):
    def make(self):
        self.now = 0.0
        sampler = cap.Sampler(proc=Path("/nonexistent"), cgroup=Path("/nonexistent"), clock=lambda: self.now)
        sampler.cores = lambda: 8.0  # type: ignore[method-assign]
        sampler.memory = lambda: (0.3, 11 * GIB, 16 * GIB)  # type: ignore[method-assign]
        sampler.cpu = lambda: 0.3  # type: ignore[method-assign]
        return cap.Capacity(sampler, clock=lambda: self.now)

    def test_inference_busy_is_the_share_of_the_time_the_slot_was_used_and_late_cameras_are_counted_for_a_while(self):
        capacity = self.make()
        capacity.evaluate(analysed=4, demand=10)  # Starts the controller
        self.now = 10.0
        capacity.note_inference(7.5)
        capacity.note_late("a")
        capacity.note_late("b")
        sample = capacity.sample(analysed=4)
        self.assertAlmostEqual(sample.inference_busy, 0.75)
        self.assertEqual(sample.late_share, 0.5)
        self.now = 10.0 + cap.LATE_SECONDS + 1
        self.assertEqual(capacity.sample(analysed=4).late_share, 0.0)

    def test_delays_on_a_lightly_loaded_machine_are_not_a_capacity_signal(self):
        capacity = self.make()
        capacity.evaluate(analysed=6, demand=7)
        self.now = 10.0
        capacity.note_inference(2.0)  # 20% busy, CPU 30%: the machine has room
        capacity.note_late("a")
        capacity.note_late("b")
        sample = capacity.sample(analysed=6)
        self.assertEqual(sample.late_share, 0.0)
        self.assertIn("a", capacity.late)

    def test_the_first_evaluation_starts_carefully_and_later_ones_use_the_measurements(self):
        capacity = self.make()
        first = capacity.evaluate(analysed=0, demand=34, last_limit=40)
        self.assertEqual((first.action, first.limit), ("start", 30))
        status = capacity.status("auto", first.limit, analysed=0, wanting=34)
        self.assertEqual((status["mode"], status["limit_cameras"], status["cores"], status["memory_total_bytes"]), ("auto", 30, 8.0, 16 * GIB))
        self.now = 15.0
        capacity.note_inference(14.0)  # The slot was busy 93% of the time
        decision = capacity.evaluate(analysed=30, demand=34)
        self.assertEqual((decision.action, decision.factor), ("shed", "inference"))
        self.assertLess(decision.limit, 30)


# --- the worker's use of it ---

import asyncio
import json
import os
import uuid
from unittest import mock

from app import detection
from app.credentials import encrypt
from app.store import connect, initialize


class FakeSampler(cap.Sampler):
    """A machine whose load the test sets."""

    def __init__(self) -> None:
        super().__init__(proc=Path("/nonexistent"), cgroup=Path("/nonexistent"))
        self.cpu_value: float | None = 0.3

    def cores(self) -> float:
        return 8.0

    def cpu(self) -> float | None:
        return self.cpu_value

    def memory(self):
        return 0.3, 11 * GIB, 16 * GIB


class QuietDetector:
    def infer(self, frame, threshold):
        return []


async def never_frames(source, fps):
    await asyncio.Event().wait()  # The camera is connected and sends nothing
    yield b""


class ManagerCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")}
        os.environ.update({"NVR_DB_PATH": str(root / "s.sqlite3"), "NVR_RECORDINGS_PATH": str(root / "r"), "NVR_CREDENTIAL_KEY_PATH": str(root / "k")})
        (root / "r").mkdir()
        initialize()
        self.addCleanup(self.restore)
        self.cameras: list[str] = []
        self.now = [0.0]
        self.sampler = FakeSampler()

    def restore(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def add_cameras(self, names: list[str]) -> list[str]:
        ids = []
        with connect() as db:
            for name in names:
                camera_id = str(uuid.uuid4())
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (camera_id, name, encrypt("rtsp://camera.invalid/x")))
                db.execute("INSERT INTO ai_camera_settings(camera_id,config_override,enabled,use_substream) VALUES(?,1,1,0)", (camera_id,))
                ids.append(camera_id)
        self.cameras += ids
        return ids

    def manager(self) -> detection.DetectionManager:
        manager = detection.DetectionManager()
        manager.detector = QuietDetector()
        manager.capacity = cap.Capacity(self.sampler, clock=lambda: self.now[0])
        return manager

    def run_async(self, body):
        async def wrapper():
            manager = self.manager()
            with mock.patch("app.detection._raw_frames", never_frames):
                try:
                    return await body(manager)
                finally:
                    manager.stopping = True
                    for task in list(manager.tasks.values()):
                        task.cancel()
                    await asyncio.gather(*manager.tasks.values(), return_exceptions=True)

        return asyncio.run(wrapper())

    def active(self, manager) -> int:
        return sum(1 for task in manager.tasks.values() if not task.done())

    def states(self) -> dict[str, tuple[str, str | None]]:
        with connect() as db:
            return {row["camera_id"]: (row["status"], row["last_error"]) for row in db.execute("SELECT camera_id,status,last_error FROM ai_camera_settings")}

    def status(self):
        with connect() as db:
            return dict(db.execute("SELECT * FROM ai_capacity_status WHERE singleton_id=1").fetchone())


class ManagerTests(ManagerCase):
    def test_automatic_mode_starts_with_a_few_cameras_says_why_the_rest_wait_and_records_its_status(self):
        self.add_cameras([f"Camera {index:02d}" for index in range(10)])

        async def body(manager):
            await manager.reconcile()
            return self.active(manager)

        self.assertEqual(self.run_async(body), cap.START_CAMERAS)
        waiting = [error for state, error in self.states().values() if state == "skipped"]
        self.assertEqual(len(waiting), 6)
        self.assertTrue(all(error.startswith("Waiting: this machine is at its limit") for error in waiting))
        status = self.status()
        self.assertEqual((status["mode"], status["limit_cameras"], status["analysed"], status["wanting"], status["cores"]), ("auto", cap.START_CAMERAS, 4, 10, 8.0))

    def test_with_headroom_it_adds_a_camera_after_a_minute_and_with_pressure_it_takes_cameras_away_at_once(self):
        self.add_cameras([f"Camera {index:02d}" for index in range(12)])

        async def body(manager):
            await manager.reconcile()
            reached = [self.active(manager)]
            for _ in range(cap.GROW_AFTER + 1):
                self.now[0] += cap.EVALUATE_SECONDS
                await manager.reconcile()
            reached.append(self.active(manager))
            self.sampler.cpu_value = 0.97
            self.now[0] += cap.EVALUATE_SECONDS
            await manager.reconcile()
            reached.append(self.active(manager))
            return reached

        before, grown, shed = self.run_async(body)
        self.assertEqual((before, grown), (4, 5))
        self.assertLess(shed, grown)
        waiting = [error for state, error in self.states().values() if state == "skipped"]
        self.assertTrue(any("(CPU)" in error for error in waiting))
        self.assertEqual(self.status()["action"], "shed")
        self.assertEqual(self.status()["factor"], "cpu")

    def test_the_cameras_that_matter_most_are_analysed_first(self):
        ids = self.add_cameras([f"Camera {index:02d}" for index in range(10)])
        important = ids[9]  # Last by name
        with connect() as db:
            db.execute("INSERT INTO rules(id,name,camera_ids_json,condition_type,condition_json,classes_json,active_from) VALUES('r1','Gate',?,'zone_entry',?,?,'2026-01-01T00:00:00Z')",
                       (json.dumps([important]), '{"zone":{}}', '["person"]'))
            db.execute("INSERT INTO fall_settings(camera_id,enabled) VALUES(?,1)", (ids[8],))

        async def body(manager):
            await manager.reconcile()
            return {camera_id for camera_id, task in manager.tasks.items() if not task.done()}

        running = self.run_async(body)
        self.assertIn(important, running)
        self.assertIn(ids[8], running)
        self.assertEqual(len(running), cap.START_CAMERAS)

    def test_a_fixed_number_is_the_limit_whatever_the_machine_is_doing_and_has_no_cap_of_sixteen(self):
        self.add_cameras([f"Camera {index:02d}" for index in range(20)])
        with connect() as db:
            db.execute("UPDATE ai_runtime_settings SET capacity_mode='fixed',max_active_cameras=3")
        self.sampler.cpu_value = 0.99

        async def body(manager):
            await manager.reconcile()
            return self.active(manager)

        self.assertEqual(self.run_async(body), 3)
        self.assertTrue(any(error == "Waiting because the fixed limit of 3 analysed cameras is in use" for _, error in self.states().values()))
        with connect() as db:
            db.execute("UPDATE ai_runtime_settings SET max_active_cameras=40")
        self.assertEqual(self.run_async(body), 20)  # More than sixteen, all of them

    def test_after_a_restart_it_resumes_a_little_below_where_it_was(self):
        self.add_cameras([f"Camera {index:02d}" for index in range(40)])
        with connect() as db:
            db.execute("INSERT INTO ai_capacity_status(singleton_id,mode,limit_cameras,updated_at) VALUES(1,'auto',24,'2026-01-01T00:00:00+00:00')")

        async def body(manager):
            await manager.reconcile()
            return self.active(manager)

        self.assertEqual(self.run_async(body), 18)

    def test_inference_time_and_late_cameras_reach_the_controller(self):
        manager = self.manager()
        manager.capacity.note_inference(1.0)
        manager.capacity.note_late("a")
        self.assertEqual(manager.capacity.busy_seconds, 1.0)
        self.assertIn("a", manager.capacity.late)


class SchemaTests(ManagerCase):
    def test_the_limit_of_sixteen_is_gone_from_the_database_and_existing_settings_become_automatic(self):
        with connect() as db:
            db.execute("UPDATE ai_runtime_settings SET max_active_cameras=500")  # Would have been refused before
            row = db.execute("SELECT max_active_cameras,capacity_mode FROM ai_runtime_settings").fetchone()
            self.assertEqual((row["max_active_cameras"], row["capacity_mode"]), (500, "auto"))
            with self.assertRaises(Exception):
                db.execute("UPDATE ai_runtime_settings SET max_active_cameras=0")
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='ai_capacity_status'").fetchone())


class UpgradeTests(unittest.TestCase):
    def test_an_existing_installation_keeps_its_settings_and_becomes_automatic(self):
        from app import migrations

        with tempfile.TemporaryDirectory() as directory:
            previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_RECORDINGS_PATH")}
            os.environ.update({"NVR_DB_PATH": str(Path(directory) / "s.sqlite3"), "NVR_CREDENTIAL_KEY_PATH": str(Path(directory) / "k"), "NVR_RECORDINGS_PATH": directory})
            try:
                before = [(version, module) for version, module in migrations.MIGRATIONS if version <= 50]
                with mock.patch.object(migrations, "MIGRATIONS", before):
                    initialize()
                with connect() as db:
                    db.execute("UPDATE ai_runtime_settings SET max_active_cameras=7,provider='CPU',validated_at='2026-01-01T00:00:00Z'")
                    with self.assertRaises(Exception):
                        db.execute("UPDATE ai_runtime_settings SET max_active_cameras=17")  # The old limit
                initialize()
                with connect() as db:
                    row = db.execute("SELECT * FROM ai_runtime_settings").fetchone()
                    self.assertEqual((row["max_active_cameras"], row["capacity_mode"], row["provider"], row["validated_at"]), (7, "auto", "CPU", "2026-01-01T00:00:00Z"))
                    db.execute("UPDATE ai_runtime_settings SET max_active_cameras=17")
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


from tests.test_access_grants import GrantCase


class ApiTests(GrantCase):
    def setUp(self):
        super().setUp()
        self.app.state.detectors = mock.Mock(reconcile=mock.AsyncMock())  # Without workers there is no analysis manager to nudge

    def audit_rows(self, action):
        with connect() as db:
            return [json.loads(row["details_json"]) for row in db.execute("SELECT details_json FROM audit_log WHERE action=?", (action,))]

    def test_the_default_is_automatic_and_a_number_is_a_fixed_limit_with_no_ceiling_of_sixteen(self):
        shown = self.admin.get("/api/ai/settings").json()
        self.assertEqual(shown["capacity_mode"], "auto")
        self.assertIsNone(shown["capacity"])  # The worker has not reported yet
        fixed = self.admin.put("/api/ai/settings", json={"max_active_cameras": 40})  # A number alone: fixed, as before
        self.assertEqual(fixed.status_code, 200, fixed.text)
        self.assertEqual((fixed.json()["capacity_mode"], fixed.json()["max_active_cameras"]), ("fixed", 40))
        auto = self.admin.put("/api/ai/settings", json={"max_active_cameras": 40, "capacity_mode": "auto"})
        self.assertEqual((auto.json()["capacity_mode"], auto.json()["max_active_cameras"]), ("auto", 40))
        self.assertEqual(self.admin.put("/api/ai/settings", json={"max_active_cameras": 0}).status_code, 422)
        self.assertEqual(self.admin.put("/api/ai/settings", json={"max_active_cameras": 5, "capacity_mode": "sometimes"}).status_code, 422)
        self.assertEqual([item["capacity_mode"] for item in self.audit_rows("ai_runtime_configured")], ["fixed", "auto"])

    def test_the_workers_status_is_shown_and_only_administrators_change_the_mode(self):
        with connect() as db:
            db.execute("INSERT INTO ai_capacity_status(singleton_id,mode,limit_cameras,analysed,wanting,factor,cpu,cores,inference_busy,memory_used_bytes,memory_total_bytes,updated_at) "
                       "VALUES(1,'auto',22,22,34,'cpu',0.78,8,0.52,5000000000,16000000000,'2026-11-03T12:00:00+00:00')")
        capacity = self.admin.get("/api/ai/settings").json()["capacity"]
        self.assertEqual((capacity["limit_cameras"], capacity["analysed"], capacity["wanting"], capacity["factor"], capacity["cores"]), (22, 22, 34, "cpu", 8.0))
        self.assertEqual(self.limited.put("/api/ai/settings", json={"max_active_cameras": 2}).status_code, 403)
