"""Browser sessions remember which phone produced them (#294).

A phone's device secret is exchanged for an ordinary browser session. Until now nothing linked the two, so taking a phone away stopped it making new sessions
and left the ones it had made working until they expired. ``sessions.device_session_id`` names the device, and deleting the device (revoked by id or by its secret,
pushed out by newer phones, or its account removed) deletes those sessions in the same statement, through the foreign key.

Sessions already open when this runs have no device and are left alone: they end on their own, within the session lifetime.
"""
from __future__ import annotations

import sqlite3

NAME = "session_device"


def upgrade(db: sqlite3.Connection) -> None:
    if "device_session_id" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
        db.execute("ALTER TABLE sessions ADD COLUMN device_session_id TEXT REFERENCES device_sessions(id) ON DELETE CASCADE")
    db.execute("CREATE INDEX IF NOT EXISTS sessions_device_idx ON sessions(device_session_id) WHERE device_session_id IS NOT NULL")
