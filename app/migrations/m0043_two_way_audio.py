"""Two-way audio (#147): who may talk through a camera's speaker, and the record of each talk.

``camera_talk`` is a camera's talk setting: whether talking is on, which roles may talk, and whether the talk audio is kept. A camera with no row has talking off.
``talk_sessions`` holds one row per talk: who, when, how long and why it ended. The audio is kept in ``audio_wav`` only when the camera's setting said to.
"""
from __future__ import annotations

import sqlite3

NAME = "two_way_audio"

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS camera_talk (
        camera_id TEXT PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
        roles_json TEXT NOT NULL DEFAULT '["administrator"]',
        record_audio INTEGER NOT NULL DEFAULT 0 CHECK (record_audio IN (0,1)),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS talk_sessions (
        id TEXT PRIMARY KEY,
        camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        user_id INTEGER,
        username TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        seconds REAL,
        ended_reason TEXT,
        codec TEXT,
        audio_wav BLOB
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_talk_sessions_camera ON talk_sessions(camera_id, started_at DESC)",
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)


def verify(db: sqlite3.Connection) -> None:
    for table in ("camera_talk", "talk_sessions"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            raise RuntimeError(f"{table} was not created")
