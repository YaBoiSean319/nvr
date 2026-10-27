"""Camera capability health: how each thing a camera can do is working, judged separately.

A camera whose motion events fail still has working video, so each capability (discovery, authentication, video, audio, motion, PTZ) has its
own row with a plain-language message. ``failures`` and ``next_retry_at`` drive the bounded, backed-off recheck of failed capabilities.
"""
from __future__ import annotations

import sqlite3

NAME = "camera_capabilities"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE camera_capabilities (
            camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
            capability TEXT NOT NULL CHECK (capability IN ('discovery','authentication','video','audio','motion','ptz')),
            state TEXT NOT NULL CHECK (state IN ('ok','available','unsupported','failed','unknown')),
            message TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL,
            failures INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT,
            PRIMARY KEY (camera_id, capability)
        )
        """
    )
    db.execute("CREATE INDEX camera_capabilities_retry ON camera_capabilities(next_retry_at) WHERE next_retry_at IS NOT NULL")
