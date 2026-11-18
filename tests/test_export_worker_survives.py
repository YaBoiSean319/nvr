"""One failed database call does not stop the export worker for good (#357).

A "database is locked" while a job was claimed or finished ended the worker's task, then the poll that feeds it, while the exporter process
kept reporting healthy: every later export stayed queued until the container was restarted."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import exports
from app.store import connect, initialize

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")


class ExportWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in _ENV}
        root = Path(self.temporary.name)
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        initialize()
        self.managers: list[exports.ExportManager] = []

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.stop()

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def job(self, job_id: str, status: str = "queued") -> None:
        with connect() as db:
            db.execute("INSERT INTO exports(id,requested_by,camera_ids_json,requested_start,requested_end,status,created_at,updated_at) VALUES(?,NULL,?,?,?,?,?,?)",
                       (job_id, json.dumps([]), "2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00", status, f"2026-01-01T00:00:0{job_id[-1]}+00:00", "2026-01-01T00:00:00+00:00"))

    def status(self, job_id: str) -> tuple[str, str | None]:
        with connect() as db:
            row = db.execute("SELECT status,error FROM exports WHERE id=?", (job_id,)).fetchone()
        return row["status"], row["error"]

    async def manager(self, execute) -> exports.ExportManager:
        manager = exports.ExportManager()
        self.enterContext(patch.object(exports, "POLL_SECONDS", 0.05))
        self.enterContext(patch.object(manager, "_execute", execute))
        await manager.start()
        self.managers.append(manager)
        return manager

    async def until(self, condition, seconds: float = 5.0) -> None:
        for _ in range(int(seconds / 0.02)):
            if condition():
                return
            await asyncio.sleep(0.02)
        self.fail("timed out waiting")

    async def test_an_error_while_one_job_is_run_leaves_the_worker_running_for_the_next(self):
        self.job("job-1")
        self.job("job-2")
        ran: list[str] = []

        async def execute(job_id: str) -> None:
            ran.append(job_id)
            if len(ran) == 1:
                raise sqlite3.OperationalError("database is locked")
            with connect() as db:
                db.execute("UPDATE exports SET status='completed' WHERE id=?", (job_id,))

        with self.assertLogs(exports.__name__, level="ERROR"):
            manager = await self.manager(execute)
            await self.until(lambda: self.status("job-1")[0] == "completed" and self.status("job-2")[0] == "completed")
        self.assertFalse(manager._worker.done())
        self.assertFalse(manager._poll_task.done())
        self.assertEqual(ran[:2], ["job-1", "job-2"])  # The first is tried again by the poll, having stayed queued

    async def test_a_worker_task_that_has_ended_is_started_again_by_the_poll(self):
        async def execute(job_id: str) -> None:
            with connect() as db:
                db.execute("UPDATE exports SET status='completed' WHERE id=?", (job_id,))

        manager = await self.manager(execute)
        manager._worker.cancel()
        await asyncio.gather(manager._worker, return_exceptions=True)
        self.job("job-1")
        with self.assertLogs(exports.__name__, level="ERROR"):
            await self.until(lambda: self.status("job-1")[0] == "completed")
        self.assertFalse(manager._worker.done())

    async def test_a_job_left_running_with_nothing_running_is_failed_and_says_so(self):
        async def execute(job_id: str) -> None:
            return None

        await self.manager(execute)
        self.job("job-1", status="running")  # Its end could not be recorded, and nothing is rendering it
        await self.until(lambda: self.status("job-1")[0] == "failed")
        self.assertEqual(self.status("job-1")[1], "Export was interrupted by an internal error; start it again")
        with connect() as db:
            self.assertEqual(db.execute("SELECT details_json FROM audit_log WHERE action='export_failed' AND resource_id='job-1'").fetchone()[0], '{"reason":"worker_error"}')

    async def test_progress_that_cannot_be_written_does_not_stop_ffmpegs_output_being_read(self):
        manager = exports.ExportManager()
        with patch.object(manager, "_progress", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                manager._advance_progress("job-1", True)  # What the reader catches for each progress line


if __name__ == "__main__":
    unittest.main()
