"""Bulk Add: a camera in a bulk-add job can be a device found by discovery (#173).

Several discovered devices are added with one shared login as one job. The item keeps which discovery record it came from, so the job takes the
device's address and ONVIF service from that record, claims it while it is being added (as adding one discovered device does), and links the
new camera to it.
"""
from __future__ import annotations

import sqlite3

NAME = "camera_add_candidates"


def upgrade(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(camera_add_items)")}
    if "candidate_id" not in columns:
        db.execute("ALTER TABLE camera_add_items ADD COLUMN candidate_id TEXT")
