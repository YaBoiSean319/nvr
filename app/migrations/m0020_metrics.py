"""Counters behind the Prometheus ``/metrics`` endpoint: what the recorder did, which no other table keeps.

Dropped segments and the cumulative segment write latency buckets live here, written by the recorder process
and read by the web process. Everything else the endpoint reports is read from tables that already exist.
"""
from __future__ import annotations

import sqlite3

NAME = "metrics"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE metric_counters (
            name TEXT NOT NULL,
            camera_id TEXT NOT NULL DEFAULT '',
            value REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (name, camera_id)
        )
        """
    )
