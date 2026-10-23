from __future__ import annotations

import os
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app.migrations as migrations
from app.migrations import m0001_baseline
from app.store import connect, database_path, initialize

FIXTURES = Path(__file__).parent / "fixtures"


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("NVR_DB_PATH")
        self.path = Path(self.temporary.name) / "state.sqlite3"
        os.environ["NVR_DB_PATH"] = str(self.path)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.temporary.cleanup()

    def _load(self, script: str) -> None:
        db = sqlite3.connect(self.path, isolation_level=None)
        try:
            db.executescript(script)
        finally:
            db.close()

    def _versions(self) -> list[int]:
        with connect() as db:
            return [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")]

    def _copies(self) -> list[Path]:
        return sorted(self.path.parent.glob(f"{self.path.name}.pre-migration-*"))

    def _rows(self, db: sqlite3.Connection, table: str) -> list[tuple]:
        return [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def test_fresh_database_is_created_by_migrations_and_rerun_is_a_no_op(self):
        initialize()
        self.assertEqual(self._versions(), [1])
        self.assertEqual(self._copies(), [])
        initialize()
        self.assertEqual(self._versions(), [1])
        with connect() as db:
            m0001_baseline.verify(db)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_unversioned_current_schema_is_stamped_without_changing_data(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        m0001_baseline.upgrade(db)
        db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
        db.execute("INSERT INTO camera_groups(id,name) VALUES('g1','Dock')")
        db.execute(
            "INSERT INTO cameras(id,name,stream_url_ciphertext,created_at,updated_at,group_id) "
            "VALUES('c1','Dock East',x'00','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','g1')"
        )
        db.execute("INSERT INTO camera_group_memberships(group_id,camera_id) VALUES('g1','c1')")
        before = {table: self._rows(db, table) for table in ("users", "camera_groups", "cameras", "camera_group_memberships")}
        db.close()

        initialize()

        self.assertEqual(self._versions(), [1])
        with connect() as db:
            after = {table: self._rows(db, table) for table in before}
        self.assertEqual(after, before)
        self.assertEqual(len(self._copies()), 1)

    def test_legacy_databases_upgrade_and_keep_their_records(self):
        for fixture in ("legacy_single_camera.sql", "legacy_grouped_motion.sql"):
            with self.subTest(fixture=fixture):
                for leftover in self.path.parent.iterdir():
                    leftover.unlink()
                self._load((FIXTURES / fixture).read_text())

                initialize()

                self.assertEqual(self._versions(), [1])
                with connect() as db:
                    m0001_baseline.verify(db)
                    self.assertEqual(db.execute("SELECT username FROM users WHERE id=1").fetchone()[0], "fixture-admin")
                    session = db.execute("SELECT session_id,user_id FROM sessions").fetchone()
                    self.assertEqual(session["user_id"], 1)
                    self.assertTrue(session["session_id"])
                    camera = db.execute("SELECT name,recording_enabled FROM cameras WHERE id='camera-a'").fetchone()
                    self.assertEqual(camera["name"], "Fixture Camera A")
                    segment = db.execute("SELECT relative_path FROM recording_segments WHERE camera_id='camera-a'").fetchone()
                    self.assertEqual(segment["relative_path"], "camera-a/2026/01/01/000000.mp4")
                    if fixture == "legacy_grouped_motion.sql":
                        membership = db.execute("SELECT group_id FROM camera_group_memberships WHERE camera_id='camera-a'").fetchone()
                        self.assertEqual(membership["group_id"], "group-lobby")
                copies = self._copies()
                self.assertEqual(len(copies), 1)
                copy = sqlite3.connect(copies[0])
                try:
                    tables = {row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                finally:
                    copy.close()
                self.assertNotIn("schema_migrations", tables)
                self.assertEqual(copies[0].stat().st_mode & 0o777, 0o600)

    def test_upgraded_legacy_database_serves_the_application(self):
        self._load((FIXTURES / "legacy_grouped_motion.sql").read_text())
        from app.main import app

        with TestClient(app) as client:
            status = client.get("/api/setup/status")
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["setup_required"])
        self.assertEqual(self._versions(), [1])

    def test_unrecognised_database_is_refused_untouched(self):
        self._load("CREATE TABLE inventory(id INTEGER PRIMARY KEY); INSERT INTO inventory VALUES(7);")
        original = self.path.read_bytes()

        with self.assertRaisesRegex(migrations.MigrationError, "not a recognised NVR database"):
            initialize()

        db = sqlite3.connect(self.path)
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            db.close()
        self.assertEqual(tables, {"inventory"})
        self.assertEqual(self._copies(), [])
        self.assertEqual(self.path.read_bytes()[:100], original[:100])

    def test_database_that_cannot_reach_the_baseline_is_not_stamped(self):
        self._load(
            "CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, role TEXT, created_at TEXT);"
            "CREATE TABLE ai_runtime_settings(singleton_id INTEGER PRIMARY KEY);"
        )
        with self.assertRaises(migrations.MigrationError):
            initialize()
        db = sqlite3.connect(self.path)
        try:
            recorded = db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='schema_migrations'"
            ).fetchone()[0] and db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        finally:
            db.close()
        self.assertFalse(recorded)
        self.assertEqual(len(self._copies()), 1)

    def test_newer_schema_is_refused(self):
        initialize()
        with connect() as db:
            db.execute("INSERT INTO schema_migrations(version,name,applied_at) VALUES(999,'future','2030-01-01T00:00:00Z')")
        with self.assertRaisesRegex(migrations.MigrationError, "newer release"):
            initialize()

    def test_failed_migration_rolls_back_and_keeps_the_copy(self):
        initialize()
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")

        def broken(db):
            db.execute("CREATE TABLE half_applied(id INTEGER)")
            db.execute("UPDATE users SET username='changed'")
            raise ValueError("deliberate failure")

        failing = types.SimpleNamespace(NAME="deliberate", upgrade=broken)
        with mock.patch.object(migrations, "MIGRATIONS", [*migrations.MIGRATIONS, (2, failing)]):
            with self.assertRaisesRegex(migrations.MigrationError, "Schema migration 2 .*pre-migration copy"):
                initialize()

        self.assertEqual(self._versions(), [1])
        with connect() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='half_applied'").fetchone())
            self.assertEqual(db.execute("SELECT username FROM users WHERE id=1").fetchone()[0], "admin")
        self.assertEqual(len(self._copies()), 1)

    def test_pre_migration_hook_runs_and_copies_are_bounded(self):
        initialize()
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
        calls = []
        for version in range(2, 7):
            step = types.SimpleNamespace(NAME=f"step{version}", upgrade=lambda db: None)
            with mock.patch.object(migrations, "MIGRATIONS", [*migrations.MIGRATIONS, *[
                (number, types.SimpleNamespace(NAME=f"step{number}", upgrade=lambda db: None)) for number in range(2, version)
            ], (version, step)]), mock.patch.object(migrations, "pre_migration_hooks", [calls.append]):
                initialize()
        self.assertEqual(calls, [database_path()] * 5)
        self.assertEqual(len(self._copies()), migrations.PRE_MIGRATION_COPIES_KEPT)


if __name__ == "__main__":
    unittest.main()
