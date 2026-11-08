"""Face watch (#253), part 2: the watch list, and the switches that keep it off until an administrator turns it on.

Nothing here recognises anyone by itself. ``face_watch_entries`` are the people someone chose to put on the list (with a reason and an optional expiry), ``face_embeddings`` the
numbers that describe each one's face (several per entry make a match likelier), ``face_camera_settings`` which cameras look for faces (none, until switched on one by one),
and ``face_settings`` how alike two faces must be to count as the same person. Deleting an entry deletes its numbers and its picture.
"""
from __future__ import annotations

import sqlite3

NAME = "face_watch"

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS face_watch_entries (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        reason TEXT NOT NULL,
        expires_at TEXT,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
        picture BLOB,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS face_embeddings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id TEXT NOT NULL REFERENCES face_watch_entries(id) ON DELETE CASCADE,
        embedding BLOB NOT NULL,
        camera_id TEXT,
        source_at TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS face_embeddings_entry_idx ON face_embeddings(entry_id)",
    """
    CREATE TABLE IF NOT EXISTS face_camera_settings (
        camera_id TEXT PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS face_settings (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
        match_threshold REAL NOT NULL DEFAULT 0.45 CHECK (match_threshold BETWEEN 0.25 AND 0.9),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "INSERT OR IGNORE INTO face_settings(singleton_id,match_threshold) VALUES(1,0.45)",
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)


def verify(db: sqlite3.Connection) -> None:
    for table in ("face_watch_entries", "face_embeddings", "face_camera_settings", "face_settings"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            raise RuntimeError(f"{table} was not created")
    if db.execute("SELECT 1 FROM face_settings WHERE singleton_id=1").fetchone() is None:
        raise RuntimeError("face_settings has no row")
