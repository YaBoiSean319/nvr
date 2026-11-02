"""Storage headroom (#219): the free space each storage volume keeps, by deleting the oldest footage when it is not there.

``storage_headroom`` holds a volume's chosen headroom (NULL: the default, a share of the volume) and what the last check found; ``storage_headroom_removals`` is the log of footage
removed to keep it, which the settings page reports beside the setting.
"""
from __future__ import annotations

import sqlite3

NAME = "storage_headroom"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS storage_headroom ("
        "volume_id TEXT PRIMARY KEY, "
        "headroom_bytes INTEGER CHECK (headroom_bytes IS NULL OR headroom_bytes >= 0), "
        "last_checked_at TEXT, "
        "short_bytes INTEGER NOT NULL DEFAULT 0, "
        "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS storage_headroom_removals ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "volume_id TEXT NOT NULL, "
        "removed_at TEXT NOT NULL, "
        "segments INTEGER NOT NULL, "
        "bytes INTEGER NOT NULL, "
        "newest_removed_at TEXT NOT NULL)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS storage_headroom_removals_idx ON storage_headroom_removals(volume_id, removed_at)")
