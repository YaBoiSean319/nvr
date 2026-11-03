"""Maintenance windows (#234): periods in which a camera, a group of cameras or the whole site is being worked on and raises no outward alerts.

``alarms`` remember the window they were raised in (they are still kept, just silent), and ``incidents`` remember that their notification was held back so it can be
sent when the window ends if the fault is still there.
"""
from __future__ import annotations

import sqlite3

NAME = "maintenance_windows"

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS maintenance_windows (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        scope TEXT NOT NULL CHECK (scope IN ('cameras','groups','site')),
        camera_ids_json TEXT NOT NULL DEFAULT '[]',
        group_ids_json TEXT NOT NULL DEFAULT '[]',
        starts_at TEXT NOT NULL,
        ends_at TEXT NOT NULL,
        ended_at TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_by_name TEXT,
        suppressed_alarms INTEGER NOT NULL DEFAULT 0,
        suppressed_incidents INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS maintenance_windows_time_idx ON maintenance_windows(starts_at, ends_at)",
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
    for table, column, definition in (("alarms", "maintenance_window_id", "TEXT"), ("alarms", "maintenance_name", "TEXT"), ("incidents", "held_by_window", "TEXT")):
        if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def verify(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_windows'").fetchone() is None:
        raise RuntimeError("maintenance_windows was not created")
    for table, column in (("alarms", "maintenance_window_id"), ("alarms", "maintenance_name"), ("incidents", "held_by_window")):
        if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            raise RuntimeError(f"{table}.{column} was not added")
