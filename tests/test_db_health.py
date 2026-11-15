"""The database health check, its self-test integration, the damaged-file error response and the startup line (#321)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")


def _populate(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE readings(id INTEGER PRIMARY KEY, body TEXT)")
    db.execute("CREATE INDEX readings_body ON readings(body)")
    db.executemany("INSERT INTO readings(body) VALUES(?)", [(f"row {i} " + "x" * 200,) for i in range(4000)])
    db.commit()
    db.close()


def _damage_a_data_page(path: Path) -> None:
    """Overwrite two pages in the middle of the file with garbage, as a failing disk or an interrupted write might."""
    size = path.stat().st_size
    with path.open("r+b") as handle:
        handle.seek((size // 4096 // 2) * 4096)
        handle.write(os.urandom(8192))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_a_healthy_file_passes_both_checks(self):
        from app import db_health

        path = self.root / "good.sqlite3"
        _populate(path)
        for full in (True, False):
            outcome = db_health.check(full=full, path=path)
            self.assertTrue(outcome.ok, outcome.lines)
            self.assertEqual(outcome.lines, [])

    def test_a_damaged_data_page_fails_the_full_check_with_its_lines(self):
        from app import db_health

        path = self.root / "bad.sqlite3"
        _populate(path)
        _damage_a_data_page(path)
        outcome = db_health.check(full=True, path=path)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.lines)
        self.assertLessEqual(len(outcome.lines), 5)

    def test_a_file_that_is_not_a_database_fails_even_the_light_probe(self):
        from app import db_health

        path = self.root / "junk.sqlite3"
        path.write_bytes(os.urandom(16384))
        for full in (True, False):
            outcome = db_health.check(full=full, path=path)
            self.assertFalse(outcome.ok, full)
            self.assertTrue(outcome.lines)

    def test_a_busy_database_is_not_called_damaged(self):
        from app import db_health

        self.assertTrue(db_health.is_damage(sqlite3.DatabaseError("database disk image is malformed")))
        self.assertTrue(db_health.is_damage(sqlite3.DatabaseError("file is not a database")))
        for error in (sqlite3.OperationalError("database is locked"), sqlite3.IntegrityError("UNIQUE constraint failed"), sqlite3.ProgrammingError("bad")):
            self.assertFalse(db_health.is_damage(error), type(error))

    def test_the_check_takes_no_write_lock(self):
        from app import db_health

        path = self.root / "locked.sqlite3"
        _populate(path)
        writer = sqlite3.connect(path, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")  # Someone else holds the write lock for the whole check
        try:
            self.assertTrue(db_health.check(full=True, path=path).ok)
            self.assertTrue(db_health.check(full=False, path=path).ok)
        finally:
            writer.execute("ROLLBACK")
            writer.close()


class InstalledTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.environ["NVR_DB_PATH"] = str(self.root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(self.root / "credentials.key")
        from app.store import initialize

        initialize()

    def tearDown(self):
        for key in _ENV:
            os.environ.pop(key, None)
        self.temporary.cleanup()

    def incident(self, key: str):
        from app.store import connect

        with connect() as db:
            return db.execute("SELECT fault_active,summary FROM incidents WHERE incident_key=?", (key,)).fetchone()

    def test_the_scheduled_self_test_reads_every_page_and_a_manual_one_only_probes(self):
        from app import db_health, self_test

        calls = []
        real = db_health.check
        with patch.object(db_health, "check", side_effect=lambda **kwargs: (calls.append(kwargs["full"]), real(**kwargs))[1]):
            scheduled = self_test.run("scheduled")
            manual = self_test.run("manual")
        self.assertEqual(calls, [True, False])
        for result in (scheduled, manual):
            database = next(item for item in result["results"] if item["check"] == "database")
            self.assertTrue(database["passed"], database)

    def test_a_damaged_file_raises_one_incident_ends_the_run_and_recovers_when_fixed(self):
        from app import db_health, self_test

        damaged = db_health.Outcome(ok=False, full=True, seconds=1.0, lines=["Tree 5 page 5: btreeInitPage() returns error code 11", "wrong # of entries in index readings_body"])
        with patch.object(db_health, "check", return_value=damaged), self.assertLogs("app.self_test", level="ERROR") as logged:
            result = self_test.run("scheduled")
        self.assertFalse(result["passed"])
        self.assertEqual([item["check"] for item in result["results"]], ["database"])  # The other checks read the same file and are skipped
        self.assertIn("btreeInitPage", result["results"][0]["detail"])
        self.assertIn("docs/database-recovery.md", result["results"][0]["detail"])
        self.assertTrue(any("damaged" in line for line in logged.output))
        row = self.incident("self_test:database")
        self.assertEqual(row["fault_active"], 1)
        self.assertIn("damaged", row["summary"])
        recorded = self_test.latest()
        self.assertIsNotNone(recorded)
        self.assertFalse(recorded["passed"])
        healthy = self_test.run("scheduled")  # The next night
        self.assertTrue(next(item for item in healthy["results"] if item["check"] == "database")["passed"])
        self.assertEqual(self.incident("self_test:database")["fault_active"], 0)

    def test_a_damaged_file_leaves_other_checks_incidents_alone(self):
        from app import db_health, self_test
        from app.incidents import sync_incident
        from app.store import connect

        with connect() as db:
            sync_incident(db, incident_key="self_test:volume:main", camera_id=None, fault_type="self_test", summary="Self-test failed: volume", active=True, details={})
        with patch.object(db_health, "check", return_value=db_health.Outcome(ok=False, full=True, seconds=1, lines=["bad"])), self.assertLogs("app.self_test", level="ERROR"):
            self_test.run("scheduled")
        self.assertEqual(self.incident("self_test:volume:main")["fault_active"], 1)

    def test_when_the_file_cannot_record_anything_the_result_still_comes_back_and_is_logged(self):
        from app import db_health, self_test

        with patch.object(db_health, "check", return_value=db_health.Outcome(ok=False, full=True, seconds=1, lines=["file is not a database"])), \
                patch.object(self_test, "connect", side_effect=sqlite3.DatabaseError("database disk image is malformed")), \
                self.assertLogs("app.self_test", level="ERROR") as logged:
            result = self_test.run("scheduled")
        self.assertFalse(result["passed"])
        self.assertGreaterEqual(len(logged.output), 2)  # The finding, then that it could not be written

    def test_startup_names_a_damaged_file_in_one_line(self):
        from app import db_health

        with self.assertLogs("app.db_health", level="ERROR") as logged:
            with patch.object(db_health, "check", return_value=db_health.Outcome(ok=False, full=False, seconds=0, lines=["file is not a database"])):
                self.assertFalse(db_health.log_at_startup())
        self.assertEqual(len(logged.output), 1)
        self.assertIn("THE DATABASE FILE IS DAMAGED", logged.output[0])
        self.assertIn("pre-migration", logged.output[0])
        with self.assertNoLogs("app.db_health", level="ERROR"):
            self.assertTrue(db_health.log_at_startup())


class ErrorResponseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.environ["NVR_DB_PATH"] = str(self.root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(self.root / "credentials.key")
        from app.store import initialize

        initialize()

    def tearDown(self):
        for key in _ENV:
            os.environ.pop(key, None)
        self.temporary.cleanup()

    def get(self, error: Exception):
        from fastapi.testclient import TestClient
        from app.application import create_app

        from fastapi.routing import APIRoute

        app = create_app(start_workers=False)

        def broken():
            raise error

        app.router.routes.insert(0, APIRoute("/api/test-database-error", broken, methods=["GET"]))  # Ahead of the catch-all that serves the interface

        with TestClient(app, raise_server_exceptions=False) as client:
            return client.get("/api/test-database-error")

    def test_a_damaged_file_answers_503_with_a_clear_message_and_one_log_line(self):
        with self.assertLogs("app.application", level="ERROR") as logged:
            response = self.get(sqlite3.DatabaseError("database disk image is malformed"))
        self.assertEqual(response.status_code, 503)
        self.assertIn("database is damaged", response.json()["detail"])
        self.assertTrue(any("database file is damaged" in line for line in logged.output))

    def test_a_busy_database_keeps_its_ordinary_500(self):
        response = self.get(sqlite3.OperationalError("database is locked"))
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("damaged", response.text)


if __name__ == "__main__":
    unittest.main()
