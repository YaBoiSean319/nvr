"""Camera network hardening: the camera networks an administrator names, the findings, and the network probes already made.

``hardening_findings`` holds one row for each weakness a camera has right now (a finding that goes away is deleted, so one that
comes back is new), with a fingerprint of the condition that is free of secrets: an acknowledgement belongs to that fingerprint and
lapses when it changes. ``probes_enabled`` lets an administrator stop the checks asking cameras anything (the two protocol checks are then not made).
``hardening_probes`` remembers when each camera was last asked whether it offers a secure alternative, so
checks never contact a camera more often than the probe gap allows.
"""
from __future__ import annotations

import sqlite3

NAME = "hardening"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE hardening_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            camera_networks_json TEXT NOT NULL DEFAULT '[]',
            probes_enabled INTEGER NOT NULL DEFAULT 1 CHECK (probes_enabled IN (0,1)),
            last_run_at TEXT,
            last_run_kind TEXT CHECK (last_run_kind IN ('manual','scheduled')),
            scheduled_from TEXT
        )
        """
    )
    db.execute("INSERT INTO hardening_settings(id) VALUES(1)")
    db.execute(
        """
        CREATE TABLE hardening_findings (
            camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
            check_name TEXT NOT NULL CHECK (check_name IN ('default_credentials','plain_rtsp','onvif_http','outside_network')),
            severity TEXT NOT NULL CHECK (severity IN ('high','medium')),
            fingerprint TEXT NOT NULL,
            detail TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            acknowledged_at TEXT,
            acknowledged_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            acknowledged_reason TEXT,
            PRIMARY KEY (camera_id, check_name)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE hardening_probes (
            camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('rtsps','onvif_https')),
            host TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            port INTEGER,
            PRIMARY KEY (camera_id, kind)
        )
        """
    )
