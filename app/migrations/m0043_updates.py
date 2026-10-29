"""Guided updates (#148): the update-check setting and what the last check found, the pre-update step, and a history of upgrades seen at startup.

``update_settings`` is one row: whether the release check is on, when it last ran and what it found (``latest_json``, the newest release newer than this one),
the pre-update step in progress (``prepared_*``) and the version and schema the last start saw (``seen_*``). ``update_history`` has a row each time a start finds
a different version or schema from the one before: what changed and which migrations were applied.
"""
from __future__ import annotations

import sqlite3

NAME = "updates"

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS update_settings (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
        check_enabled INTEGER NOT NULL DEFAULT 1 CHECK (check_enabled IN (0,1)),
        last_checked_at TEXT,
        last_error TEXT,
        latest_json TEXT,
        prepared_at TEXT,
        prepared_run_id TEXT,
        prepared_version TEXT,
        prepared_without_backup INTEGER NOT NULL DEFAULT 0 CHECK (prepared_without_backup IN (0,1)),
        seen_version TEXT,
        seen_schema INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS update_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        from_version TEXT NOT NULL,
        to_version TEXT NOT NULL,
        from_schema INTEGER,
        to_schema INTEGER NOT NULL,
        migrations_json TEXT NOT NULL DEFAULT '[]'
    )
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
    db.execute("INSERT OR IGNORE INTO update_settings(singleton_id) VALUES(1)")


def verify(db: sqlite3.Connection) -> None:
    for table in ("update_settings", "update_history"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            raise RuntimeError(f"{table} was not created")
