"""Licence plate region (#210): which region's plate recogniser the AI worker uses.

One row: the chosen ``region`` (see ``app/plate_models.py``). A new installation reads Latin-alphabet plates from around the world; an installation whose administrator had
already put plate models in place by hand keeps using them (``custom``), so an upgrade changes nothing about what it reads.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

NAME = "plate_region"


def _hand_installed() -> bool:
    detector = Path(os.environ.get("NVR_PLATE_DETECTOR_MODEL_PATH", "/opt/nvr/models/vehicle-license-plate-detection-barrier-0106.xml"))
    recogniser = Path(os.environ.get("NVR_PLATE_RECOGNISER_MODEL_PATH", "/opt/nvr/models/license-plate-recognition-barrier-0007.xml"))
    return all(path.is_file() and path.with_suffix(".bin").is_file() for path in (detector, recogniser))


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS plate_region_settings ("
        "singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1), "
        "region TEXT NOT NULL DEFAULT 'worldwide', "
        "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
    )
    db.execute("INSERT OR IGNORE INTO plate_region_settings(singleton_id,region) VALUES(1,?)", ("custom" if _hand_installed() else "worldwide",))


def verify(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plate_region_settings'").fetchone() is None:
        raise RuntimeError("plate_region_settings was not created")
