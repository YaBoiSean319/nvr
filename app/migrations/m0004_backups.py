"""Configuration backup history, and segment availability after a restore."""
from __future__ import annotations

import sqlite3

NAME = "backups"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE backup_runs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('scheduled','manual','pre_migration')),
            status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed')),
            requested_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            started_at TEXT,
            finished_at TEXT,
            file_name TEXT,
            size_bytes INTEGER,
            error TEXT
        )
        """
    )
    db.execute("CREATE INDEX backup_runs_created_idx ON backup_runs(created_at DESC)")
    db.execute("ALTER TABLE recording_segments ADD COLUMN unavailable_at TEXT")
    db.execute("ALTER TABLE recording_segments ADD COLUMN unavailable_reason TEXT")
