"""Detection (#243): a confidence threshold for vehicles of its own, so a doorway that scores 56% is not called a vehicle while people are still found at 55%."""
from __future__ import annotations

import sqlite3

NAME = "vehicle_threshold"
TABLES = ("ai_camera_settings", "ai_group_settings")
COLUMN = "vehicle_confidence_threshold"


def upgrade(db: sqlite3.Connection) -> None:
    for table in TABLES:
        if COLUMN not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            # Empty means the default for vehicles; see detection.DEFAULT_VEHICLE_THRESHOLD.
            db.execute(f"ALTER TABLE {table} ADD COLUMN {COLUMN} REAL CHECK ({COLUMN} IS NULL OR {COLUMN} BETWEEN 0.1 AND 0.99)")


def verify(db: sqlite3.Connection) -> None:
    for table in TABLES:
        if COLUMN not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            raise RuntimeError(f"{table} has no {COLUMN}")
