"""Alarm-driven Live layouts and the video wall: each camera's neighbours, which rules
pop their alarms into Live, each person's opt-in, and video wall sessions.

A video wall signs in once with an API token limited to ``read`` and ``media``; its
session cookie names the token, so revoking or expiring the token ends the session.
"""
from __future__ import annotations

import sqlite3

NAME = "live_alarms"

_STATEMENTS = (
    """
    CREATE TABLE camera_neighbours (
        camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        neighbour_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        PRIMARY KEY (camera_id, neighbour_id),
        CHECK (camera_id <> neighbour_id)
    )
    """,
    "ALTER TABLE rules ADD COLUMN pop_into_live INTEGER NOT NULL DEFAULT 0 CHECK (pop_into_live IN (0,1))",
    # Copied from the rule when the alarm is raised, so editing the rule never changes past alarms.
    "ALTER TABLE alarms ADD COLUMN pop_into_live INTEGER NOT NULL DEFAULT 0 CHECK (pop_into_live IN (0,1))",
    "CREATE INDEX alarms_pop_in_idx ON alarms(pop_into_live, created_at, id)",
    "ALTER TABLE users ADD COLUMN live_alarm_pop_in INTEGER NOT NULL DEFAULT 0 CHECK (live_alarm_pop_in IN (0,1))",
    """
    CREATE TABLE wall_sessions (
        token_hash TEXT PRIMARY KEY,
        api_token_id TEXT NOT NULL REFERENCES api_tokens(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        expires_at TEXT NOT NULL
    )
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
