"""Fall detection: keep the body points the pose model found, so Playback can draw a skeleton over a person (the points were used for the decision and thrown away)."""
from __future__ import annotations

import sqlite3

NAME = "pose_points"


def upgrade(db: sqlite3.Connection) -> None:
    if "pose_json" not in {row[1] for row in db.execute("PRAGMA table_info(ai_detections)")}:
        # NULL for every detection no pose was read for (cameras without fall detection, vehicles, people inside a privacy mask).
        db.execute("ALTER TABLE ai_detections ADD COLUMN pose_json TEXT")


def verify(db: sqlite3.Connection) -> None:
    if "pose_json" not in {row[1] for row in db.execute("PRAGMA table_info(ai_detections)")}:
        raise RuntimeError("ai_detections has no pose_json")
