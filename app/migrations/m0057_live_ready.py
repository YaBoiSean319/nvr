"""Live video kept ready (#216): whether the NVR holds each camera's live stream open so the first viewer joins a stream that is already running."""
from __future__ import annotations

import sqlite3

NAME = "live_ready"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS live_ready_settings ("
        "singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1), "
        "mode TEXT NOT NULL DEFAULT 'all' CHECK (mode IN ('off','recent','all')), "
        "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
    )
    db.execute("INSERT OR IGNORE INTO live_ready_settings(singleton_id,mode) VALUES(1,'all')")


def verify(db: sqlite3.Connection) -> None:
    if db.execute("SELECT mode FROM live_ready_settings WHERE singleton_id=1").fetchone() is None:
        raise RuntimeError("live_ready_settings has no row")
