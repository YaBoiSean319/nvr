"""Edge backfill (#131): fill outages from the camera's own recording over ONVIF Profile G.

``cameras`` gets the opt-in, the bandwidth limit and what the NVR found out about Profile G support. A segment says where it came from
(recorded live or backfilled). ``backfill_jobs`` has one row for each outage being filled, with the point it has been filled up to, so work
resumes where it stopped.
"""
from __future__ import annotations

import sqlite3

NAME = "edge_backfill"


_STATEMENTS = (
    "ALTER TABLE cameras ADD COLUMN backfill_enabled INTEGER NOT NULL DEFAULT 0 CHECK (backfill_enabled IN (0,1))",
    "ALTER TABLE cameras ADD COLUMN backfill_limit_kbps INTEGER",
    "ALTER TABLE cameras ADD COLUMN backfill_support TEXT CHECK (backfill_support IN ('supported','unsupported'))",
    "ALTER TABLE cameras ADD COLUMN backfill_support_message TEXT",
    "ALTER TABLE cameras ADD COLUMN backfill_checked_at TEXT",
    "ALTER TABLE recording_segments ADD COLUMN source TEXT NOT NULL DEFAULT 'live' CHECK (source IN ('live','backfill'))",
    """
    CREATE TABLE backfill_jobs (
        id TEXT PRIMARY KEY,
        camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        starts_at TEXT NOT NULL,
        ends_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending','running','done','partial','failed','unsupported')),
        cursor_at TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        filled_seconds REAL NOT NULL DEFAULT 0,
        bytes_fetched INTEGER NOT NULL DEFAULT 0,
        observed_kbps INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX backfill_jobs_camera_idx ON backfill_jobs(camera_id,starts_at)",
    "CREATE INDEX backfill_jobs_due_idx ON backfill_jobs(state,next_attempt_at)",
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
