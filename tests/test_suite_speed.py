"""The shortcuts in ``tests/__init__.py`` change only what the suite costs: the server's password hashing keeps its real strength, and a copied empty
database is the same database the migrations build."""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests
from app import migrations, security
from app.store import connect, initialize


def _schema(path: Path) -> list[tuple]:
    db = sqlite3.connect(path)
    try:
        objects = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        rows = []
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            for row in db.execute(f'SELECT * FROM "{table}"').fetchall():  # noqa: S608 - names come from the schema itself
                rows.append((table, row))
        return objects + rows
    finally:
        db.close()


_VARIES = re.compile(r"^(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d\S*|[0-9a-f]{32})$")


def _without_what_differs(entries: list[tuple]) -> list[tuple]:
    """Rows as they are, apart from the moment each was written and the installation's random identity."""
    return [(entry[0], tuple("*" if isinstance(value, str) and _VARIES.match(value) else value for value in entry[1])) if len(entry) == 2 else entry
            for entry in entries]


class PasswordStrengthTests(unittest.TestCase):
    def test_the_server_hashes_with_at_least_600000_rounds(self):
        # The figure in the source, not the one this suite runs with.
        source = Path(security.__file__).read_text(encoding="utf-8")
        declared = re.search(r"^_ITERATIONS = ([\d_]+)$", source, re.MULTILINE)
        self.assertIsNotNone(declared)
        self.assertEqual(int(declared.group(1)), tests.PRODUCTION_PASSWORD_ROUNDS)
        self.assertGreaterEqual(tests.PRODUCTION_PASSWORD_ROUNDS, 600_000)

    def test_a_hash_made_at_the_server_strength_says_so_and_verifies(self):
        with mock.patch.object(security, "_ITERATIONS", tests.PRODUCTION_PASSWORD_ROUNDS):
            encoded = security.hash_password("correct horse 7")
        self.assertTrue(encoded.startswith(f"pbkdf2_sha256${tests.PRODUCTION_PASSWORD_ROUNDS}$"))
        self.assertTrue(security.verify_password("correct horse 7", encoded))
        self.assertFalse(security.verify_password("wrong horse 7", encoded))

    def test_only_the_round_count_differs_in_a_test_hash(self):
        encoded = security.hash_password("correct horse 7")
        self.assertTrue(encoded.startswith(f"pbkdf2_sha256${tests.TEST_PASSWORD_ROUNDS}$"))
        self.assertTrue(security.verify_password("correct horse 7", encoded))
        self.assertFalse(security.verify_password("wrong horse 7", encoded))


class EmptyDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def build(self, name: str) -> Path:
        path = self.root / name
        with mock.patch.dict(os.environ, {"NVR_DB_PATH": str(path)}):
            initialize()
        return path

    def test_a_copied_empty_database_is_the_one_the_migrations_build(self):
        with tests.real_migrations():
            replayed = self.build("replayed.sqlite3")
        self.build("first.sqlite3")  # Whichever of these is the first in the process, the second is a copy
        copied = self.build("copied.sqlite3")
        self.assertEqual(_without_what_differs(_schema(copied)), _without_what_differs(_schema(replayed)))
        self.assertEqual(oct(copied.stat().st_mode & 0o777), "0o600")
        with mock.patch.dict(os.environ, {"NVR_DB_PATH": str(copied)}), connect() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual({row[0] for row in db.execute("SELECT version FROM schema_migrations")}, {version for version, _ in migrations.MIGRATIONS})
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_each_copy_is_its_own_installation(self):
        identities = set()
        for name in ("one.sqlite3", "two.sqlite3", "three.sqlite3"):
            db = sqlite3.connect(self.build(name))
            try:
                identities.add(db.execute("SELECT instance_id FROM failover_settings").fetchone()[0])
            finally:
                db.close()
        self.assertEqual(len(identities), 3)

    def test_a_database_that_already_exists_goes_through_the_real_migrations(self):
        path = self.root / "older.sqlite3"
        first_version, first_module = migrations.MIGRATIONS[0]
        with mock.patch.dict(os.environ, {"NVR_DB_PATH": str(path)}):
            with mock.patch.object(migrations, "MIGRATIONS", [(first_version, first_module)]), tests.real_migrations():
                self.assertEqual(migrations.migrate(path, connect), [first_version])
            applied = migrations.migrate(path, connect)
        self.assertEqual(applied, [version for version, _ in migrations.MIGRATIONS[1:]])
        self.assertTrue(list(self.root.glob("older.sqlite3.pre-migration-*")))

    def test_a_different_list_of_migrations_is_never_served_from_the_copy(self):
        self.build("full.sqlite3")
        short = migrations.MIGRATIONS[:1]
        path = self.root / "short.sqlite3"
        with mock.patch.dict(os.environ, {"NVR_DB_PATH": str(path)}), mock.patch.object(migrations, "MIGRATIONS", short):
            self.assertEqual(migrations.migrate(path, connect), [short[0][0]])
            with connect() as db:
                self.assertEqual([row[0] for row in db.execute("SELECT version FROM schema_migrations")], [short[0][0]])


if __name__ == "__main__":
    unittest.main()
