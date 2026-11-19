"""The AI worker's 5-second check costs nothing when nothing has changed (#348).

It used to rewrite the state row of every camera it was not analysing, on its event loop, and hash the model files for every camera: 495
statements and 50 writes a pass for 25 cameras with AI switched on for none. An install that does not use AI paid that for ever."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest import mock

from app import detection
from app.detection import DetectionManager
from app.store import connect
from tests.test_access_grants import GrantCase


class IdlePassTests(GrantCase):
    def setUp(self):
        super().setUp()
        self.writes = 0
        self.on_loop: list[str] = []
        self.loop_thread: int | None = None
        real = detection.connect

        def watched():
            db = real()
            if threading.get_ident() == self.loop_thread:
                self.on_loop.append("a database connection was opened on the event loop")

            def trace(statement: str) -> None:
                if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
                    self.writes += 1
            db.set_trace_callback(trace)
            return db

        patcher = mock.patch.object(detection, "connect", watched)
        patcher.start()
        self.addCleanup(patcher.stop)

    def passes(self, count: int = 1) -> int:
        """Run that many passes of the check and return the write statements the last one made."""
        async def run() -> int:
            self.loop_thread = threading.get_ident()
            manager = DetectionManager()
            with mock.patch.object(manager, "_write_status"):  # The capacity status is one row a pass, and not what this is about
                for _ in range(count - 1):
                    await manager.reconcile()
                self.writes = 0
                await manager.reconcile()
            return self.writes

        return asyncio.run(run())

    def test_a_pass_with_ai_off_everywhere_writes_nothing_after_the_first(self):
        self.passes(1)  # The first pass may record that each camera is switched off
        self.assertEqual(self.passes(2), 0)

    def test_cameras_waiting_for_room_are_not_rewritten_either(self):
        with connect() as db:
            for camera_id in (self.d1, self.d2, self.y1):
                db.execute("INSERT INTO ai_camera_settings(camera_id,config_override,enabled) VALUES(?,1,1) ON CONFLICT(camera_id) DO UPDATE SET config_override=1,enabled=1", (camera_id,))
            db.execute("UPDATE ai_runtime_settings SET capacity_mode='fixed',max_active_cameras=1 WHERE singleton_id=1")
        self.passes(1)
        with connect() as db:
            self.assertEqual({row[0] for row in db.execute("SELECT status FROM ai_camera_settings WHERE enabled=1")} - {"skipped", "failed"}, set())
        self.assertEqual(self.passes(2), 0)

    def test_a_change_is_still_written_at_once(self):
        self.passes(1)
        with connect() as db:
            db.execute("UPDATE ai_camera_settings SET status='analyzed' WHERE camera_id=?", (self.d1,))  # As if it had been analysing, and was then switched off
        self.assertGreater(self.passes(1), 0)
        with connect() as db:
            self.assertEqual(db.execute("SELECT status FROM ai_camera_settings WHERE camera_id=?", (self.d1,)).fetchone()[0], "disabled")

    def test_the_pass_does_its_database_work_off_the_event_loop(self):
        self.passes(2)
        self.assertEqual(self.on_loop, [])


class ModelVersionTests(GrantCase):
    def test_the_model_files_are_hashed_once_until_they_change(self):
        folder = Path(self.environment["NVR_RECORDINGS_PATH"])
        xml, binary = folder / "model.xml", folder / "model.bin"
        xml.write_bytes(b"<net/>")
        binary.write_bytes(b"weights")
        with mock.patch.object(detection.hashlib, "sha256", wraps=detection.hashlib.sha256) as hashed:
            first = detection._model_version(xml)
            calls = hashed.call_count
            self.assertGreater(calls, 0)
            for _ in range(5):
                self.assertEqual(detection._model_version(xml), first)
            self.assertEqual(hashed.call_count, calls)
            binary.write_bytes(b"other weights, and a different size")
            self.assertNotEqual(detection._model_version(xml), first)
        self.assertEqual(detection._model_version(folder / "missing.xml"), "unavailable")


class SettingsReadTests(GrantCase):
    def test_reading_a_cameras_ai_settings_writes_only_the_first_time(self):
        self.assertEqual(self.admin.get(f"/api/cameras/{self.d1}/ai").status_code, 200)
        with connect() as db:
            before = db.execute("SELECT total_changes()").fetchone()[0]
        from app.api import ai_settings
        real, changes = ai_settings.connect, []

        def counted():
            db = real()
            db.set_trace_callback(lambda statement: changes.append(statement) if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) else None)
            return db

        with mock.patch.object(ai_settings, "connect", counted):
            self.assertEqual(self.admin.get(f"/api/cameras/{self.d1}/ai").status_code, 200)
        self.assertEqual(changes, [], before)
