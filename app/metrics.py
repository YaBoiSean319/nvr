"""The recorder's counters behind ``/metrics``: segments written, their latency, and segments dropped.

Written by the recorder process, read by ``prometheus.render`` in the web process. Light on imports, because the recorder uses it."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from .store import connect

logger = logging.getLogger("nvr.metrics")

# Seconds from a segment's end to it being indexed and playable.
LATENCY_BUCKETS = (1, 2, 5, 10, 30, 60, 120, 300)
SEGMENTS_WRITTEN, SEGMENTS_DROPPED = "segments_written", "segments_dropped"
LATENCY_SUM, LATENCY_BUCKET = "segment_latency_seconds_sum", "segment_latency_bucket"


def bump(db: sqlite3.Connection, name: str, camera_id: str = "", amount: float = 1) -> None:
    db.execute("INSERT INTO metric_counters(name,camera_id,value) VALUES(?,?,?) ON CONFLICT(name,camera_id) DO UPDATE SET value=value+excluded.value",
               (name, camera_id, amount))


def segment_written(db: sqlite3.Connection, camera_id: str, ends_at: datetime) -> None:
    """A segment became playable: count it and when, relative to when its footage ended."""
    latency = max(0.0, (datetime.now(timezone.utc) - ends_at).total_seconds())
    bump(db, SEGMENTS_WRITTEN, camera_id)
    bump(db, LATENCY_SUM, camera_id, latency)
    for bound in LATENCY_BUCKETS:
        if latency <= bound:
            bump(db, f"{LATENCY_BUCKET}:{bound}", camera_id)


def segment_dropped(camera_id: str) -> None:
    """A segment the recorder produced could not be kept. Never raises: a counter must not disturb recording."""
    try:
        with connect() as db:
            bump(db, SEGMENTS_DROPPED, camera_id)
    except sqlite3.Error:
        logger.warning("A dropped segment could not be counted", exc_info=True)
