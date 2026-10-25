"""Gap reasons, nightly self-tests and the weekly coverage report.

Every coverage interval that is not covered footage records a reason code (camera
offline, stream error, disk unavailable, outside schedule, recording disabled, no
motion, worker restart or unknown) beside its existing description. ``checked_at`` is
when the recorder last confirmed an open interval, so a restart can tell how long it
was not running. Existing intervals get a code from their description.
"""
from __future__ import annotations

import sqlite3

NAME = "coverage_reasons"

_STATEMENTS = (
    "ALTER TABLE coverage_intervals ADD COLUMN reason_code TEXT",
    "ALTER TABLE coverage_intervals ADD COLUMN checked_at TEXT",
    # What the recorder last saw go wrong with the stream: camera_offline, stream_error or disk_unavailable.
    "ALTER TABLE cameras ADD COLUMN stream_fault TEXT",
    """
    UPDATE coverage_intervals SET reason_code = CASE
        WHEN coverage_kind = 'covered' THEN NULL
        WHEN reason = 'Recording is disabled by camera or schedule policy' THEN 'recording_disabled'
        WHEN reason = 'Motion is armed; no event footage is currently expected' THEN 'no_motion'
        WHEN reason = 'Camera stream is unavailable' THEN 'camera_offline'
        ELSE 'unknown' END
    """,
    """
    CREATE TABLE self_test_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('scheduled','manual')),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        passed INTEGER,
        results_json TEXT NOT NULL DEFAULT '[]'
    )
    """,
    """
    CREATE TABLE coverage_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        created_at TEXT NOT NULL,
        report_json TEXT NOT NULL,
        UNIQUE(period_start, period_end)
    )
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
