"""Viewing audit: who watched which camera, live or in playback, and every export, download and snapshot.

One row per live or playback session (start, end, quality) and one per export, download or snapshot
per camera, so the per-person and per-camera reports are two groupings of the same rows. The person's
and the camera's names are kept on the row: the report outlives a renamed or removed account.
``security_settings.audit_retention_days`` is how long these rows are kept, separate from footage.
"""
from __future__ import annotations

import sqlite3

NAME = "viewing_audit"

_STATEMENTS = (
    """
    CREATE TABLE viewing_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        username TEXT NOT NULL,
        camera_id TEXT NOT NULL,
        camera_name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('live','playback','export','download','snapshot')),
        started_at TEXT NOT NULL,
        -- Null while a live session is open; a session left open by a crash is closed at its last heartbeat.
        ended_at TEXT,
        last_seen_at TEXT NOT NULL,
        quality TEXT,
        reference TEXT
    )
    """,
    "CREATE INDEX viewing_audit_started_idx ON viewing_audit(started_at)",
    "CREATE INDEX viewing_audit_user_idx ON viewing_audit(user_id, started_at)",
    "CREATE INDEX viewing_audit_camera_idx ON viewing_audit(camera_id, started_at)",
    "ALTER TABLE security_settings ADD COLUMN audit_retention_days INTEGER NOT NULL DEFAULT 365",
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
