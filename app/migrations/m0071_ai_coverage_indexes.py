"""AI coverage: indexes for reading a time range and for pruning (#351).

The record of when each camera was analysed gains rows for the life of the camera and nothing removed them; every reader scanned all of a
camera's rows. Readers now ask for the part of the chain a range touches (``app.ai_coverage.overlapping``): the existing
``(camera_id, starts_at)`` index answers that, the first index here finds a stretch left open, and the second finds what retention is due
to remove.
"""
from __future__ import annotations

import sqlite3

NAME = "ai_coverage_indexes"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS ai_coverage_open_idx ON ai_coverage(camera_id, starts_at) WHERE ends_at IS NULL")
    db.execute("CREATE INDEX IF NOT EXISTS ai_coverage_ended_idx ON ai_coverage(camera_id, ends_at) WHERE ends_at IS NOT NULL")
