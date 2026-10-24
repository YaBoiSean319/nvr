"""Per-worker liveness and restart history written by supervised processes."""
from __future__ import annotations

import sqlite3

NAME = "worker_status"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE worker_status (
            name TEXT PRIMARY KEY,
            pid INTEGER,
            state TEXT NOT NULL CHECK (state IN ('starting','running','stopping','stopped','crashed','restarting')),
            started_at TEXT,
            heartbeat_at TEXT,
            restarts INTEGER NOT NULL DEFAULT 0,
            last_exit_at TEXT,
            last_exit_code INTEGER,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """
    )
