"""Reading and pruning the record of when each camera was and was not being analysed (#351).

A camera's coverage is a chain of stretches, each closed when the next begins, so the stretches that touch a time range are those that
begin inside it plus the one before. Readers used to ask for "began before the end and ended after the start", which the index could
only answer by reading every stretch the camera ever had. Times are in the canonical stored form (``app.clock.stamp``)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .clock import stamp
from .store import has_row

COLUMNS = "camera_id,state,starts_at,ends_at,detail"
PRUNE_BATCH = 5_000  # Rows removed for one camera in one pass: a first pass over years of history must not hold the write lock for long


def overlapping(db: sqlite3.Connection, camera_id: str, start: str, end: str) -> list[sqlite3.Row]:
    """The camera's stretches that touch [start, end), in time order, reading only that part of its history."""
    # The stretch in progress when the range starts is the last one that began at or before it.
    before = db.execute("SELECT MAX(starts_at) FROM ai_coverage WHERE camera_id=? AND starts_at<=?", (camera_id, start)).fetchone()[0]
    rows = db.execute(f"SELECT {COLUMNS} FROM ai_coverage WHERE camera_id=? AND starts_at>=? AND starts_at<? AND (ends_at IS NULL OR ends_at>?) ORDER BY starts_at",
                      (camera_id, before or start, end, start)).fetchall()
    if before:
        # A stretch left open further back (the AI process died without closing it) still covers the range; there are at most a few.
        older = db.execute(f"SELECT {COLUMNS} FROM ai_coverage WHERE camera_id=? AND ends_at IS NULL AND starts_at<? ORDER BY starts_at", (camera_id, before)).fetchall()
        rows = older + rows
    return rows


def prune(db: sqlite3.Connection, camera_id: str, retention_days: int, now: datetime) -> int:
    """Remove a camera's finished stretches that ended before its retention period and that no remaining footage covers: the same rule as
    the detections they describe. Reads first (#280), and removes at most ``PRUNE_BATCH`` a pass."""
    cutoff = stamp(now.astimezone(timezone.utc) - timedelta(days=retention_days))
    due = ("FROM ai_coverage WHERE camera_id=? AND ends_at IS NOT NULL AND ends_at<? AND NOT EXISTS (SELECT 1 FROM recording_segments s "
           "WHERE s.camera_id=ai_coverage.camera_id AND s.starts_at<=ai_coverage.starts_at AND s.ends_at>ai_coverage.starts_at)")
    values: tuple[Any, ...] = (camera_id, cutoff)
    if not has_row(db, f"SELECT 1 {due} LIMIT 1", values):
        return 0
    return db.execute(f"DELETE FROM ai_coverage WHERE id IN (SELECT id {due} LIMIT {PRUNE_BATCH})", values).rowcount
