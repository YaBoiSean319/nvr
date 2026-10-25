"""Disk health: what SMART says about the disk behind each storage volume, as of its last reading.

One row per volume: whether a reading could be had (and if not, why, so the interface can say "not available"
and what to do), and when it could, the verdict and the facts behind it. Nothing identifying the disk beyond its model is kept.
"""
from __future__ import annotations

import sqlite3

NAME = "disk_health"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE disk_health (
            volume_id TEXT PRIMARY KEY,
            device TEXT,
            available INTEGER NOT NULL CHECK (available IN (0,1)),
            state TEXT NOT NULL CHECK (state IN ('healthy','warning','failing','unavailable')),
            reason TEXT,
            model TEXT,
            temperature_c INTEGER,
            power_on_hours INTEGER,
            findings_json TEXT NOT NULL DEFAULT '[]',
            checked_at TEXT NOT NULL
        )
        """
    )
