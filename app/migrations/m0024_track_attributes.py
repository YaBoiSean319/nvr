"""Attributes stored with each track, for searching across cameras.

The AI worker describes every detection (colours of the upper and lower body, or of a vehicle, and the vehicle's type when an
attribute model is installed) and keeps, on the track, the value most of its detections agreed on, the direction it travelled and a
coarse grid of the places it passed through (``zone_mask``: one bit for each cell of a 6x6 grid over the frame). Tracks recorded
before this keep NULL attributes and are never matched by an attribute filter.
"""
from __future__ import annotations

import sqlite3

NAME = "track_attributes"

_COLUMNS = (
    ("upper_colour", "TEXT"),
    ("lower_colour", "TEXT"),
    ("vehicle_colour", "TEXT"),
    ("vehicle_type", "TEXT"),
    ("direction", "TEXT"),
    ("zone_mask", "INTEGER NOT NULL DEFAULT 0"),
)


def upgrade(db: sqlite3.Connection) -> None:
    for name, definition in _COLUMNS:
        db.execute(f"ALTER TABLE ai_tracks ADD COLUMN {name} {definition}")
