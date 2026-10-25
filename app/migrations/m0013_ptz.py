"""PTZ: the control lease on each camera, saved presets, patrol tours and where each tour is."""
from __future__ import annotations

import sqlite3

NAME = "ptz"

_STATEMENTS = (
    # At most one person controls a camera; the lease lapses at expires_at unless renewed by a command.
    """
    CREATE TABLE ptz_leases (
        camera_id TEXT PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        acquired_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE ptz_presets (
        id TEXT PRIMARY KEY,
        camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        name TEXT NOT NULL COLLATE NOCASE,
        token TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        UNIQUE (camera_id, name)
    )
    """,
    """
    CREATE TABLE ptz_tours (
        id TEXT PRIMARY KEY,
        camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
        steps_json TEXT NOT NULL,
        schedule_json TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE UNIQUE INDEX ptz_tours_one_enabled ON ptz_tours(camera_id) WHERE enabled=1",
    """
    CREATE TABLE ptz_tour_state (
        tour_id TEXT PRIMARY KEY REFERENCES ptz_tours(id) ON DELETE CASCADE,
        step INTEGER NOT NULL DEFAULT 0,
        next_at TEXT,
        state TEXT NOT NULL DEFAULT 'idle' CHECK (state IN ('idle','running','paused'))
    )
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
