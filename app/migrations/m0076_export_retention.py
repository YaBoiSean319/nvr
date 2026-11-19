"""Exports: how long a finished export's package is kept, as a setting (#362).

7 days, as before, unless an administrator chooses otherwise, and never more than 30 (the owner's decision): exports count against the disk
budget shared with case packages and playback copies, and an export kept for months is footage kept outside its camera's retention.
"""
from __future__ import annotations

import sqlite3

NAME = "export_retention"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS export_settings (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
            retention_days INTEGER NOT NULL DEFAULT 7 CHECK (retention_days BETWEEN 1 AND 30),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
    """)
    db.execute("INSERT OR IGNORE INTO export_settings(singleton_id) VALUES(1)")
