"""Fisheye dewarping (#146): the lens of a 360-degree camera, and the virtual views a person keeps in their saved Live views, and the dewarped view an export renders.

A camera with a row in ``camera_lens`` is a fisheye camera: ``mount`` (ceiling, wall or floor), the lens centre and radius (fractions of the picture's width, height and
height) and the angle across the circle. Recordings stay raw. ``dewarp_json`` on a saved layout, a shared layout and a person's Live workspace holds, for each
camera in it, how it is shown: raw, a panorama, a double panorama or up to four virtual views with their pan, tilt and field of view.
"""
from __future__ import annotations

import sqlite3

NAME = "fisheye"

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS camera_lens (
        camera_id TEXT PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
        mount TEXT NOT NULL CHECK (mount IN ('ceiling','wall','floor')),
        center_x REAL NOT NULL DEFAULT 0.5 CHECK (center_x BETWEEN 0 AND 1),
        center_y REAL NOT NULL DEFAULT 0.5 CHECK (center_y BETWEEN 0 AND 1),
        radius REAL NOT NULL DEFAULT 0.5 CHECK (radius BETWEEN 0.1 AND 1),
        fov REAL NOT NULL DEFAULT 180 CHECK (fov BETWEEN 120 AND 220),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
)
_TABLES = ("viewer_layouts", "shared_viewer_layouts", "live_workspace_states")
_EXPORT_COLUMN = "ALTER TABLE exports ADD COLUMN dewarp_json TEXT"


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
    for table in _TABLES:
        if "dewarp_json" not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN dewarp_json TEXT NOT NULL DEFAULT '{{}}'")
    if "dewarp_json" not in {row[1] for row in db.execute("PRAGMA table_info(exports)")}:
        db.execute(_EXPORT_COLUMN)


def verify(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='camera_lens'").fetchone() is None:
        raise RuntimeError("camera_lens was not created")
    for table in _TABLES:
        if "dewarp_json" not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            raise RuntimeError(f"{table} has no dewarp_json column")
    if "dewarp_json" not in {row[1] for row in db.execute("PRAGMA table_info(exports)")}:
        raise RuntimeError("exports has no dewarp_json column")
