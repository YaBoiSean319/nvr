"""Occupancy rules (#254): more than a set number of people (or vehicles) in the frame, or in a zone, for a set time.

Unlike the other rules this one looks at several tracks at once, so it is worked out from a short window of stored detections rather than
from one track. An object counts at a moment if it was detected within ``PRESENT_WITHIN`` of it (the AI does not look at every frame), and
inside the zone when the rule has one. The rule fires once for each stretch of time the count stays above the limit for the whole set time.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable

PRESENT_WITHIN = 2  # seconds either side of a moment
MIN_COOLDOWN = timedelta(seconds=60)
WINDOW_MARGIN = 10  # seconds of history beyond the set time, so a stretch already under way is seen whole
FULL_FRAME = {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}


def exceedances(detections: Iterable[tuple[str, datetime, float, float]], condition: dict[str, Any]) -> list[datetime]:
    """When each stretch above the limit has lasted the set time. ``detections`` are (track id, time, centre x, centre y)."""
    zone, limit, hold = condition.get("zone"), condition["limit"], condition["for_seconds"]
    by_second: dict[int, set[str]] = defaultdict(set)
    first_at: dict[int, datetime] = {}
    for track_id, at, x, y in detections:
        if zone is not None and not (zone["x"] <= x <= zone["x"] + zone["width"] and zone["y"] <= y <= zone["y"] + zone["height"]):
            continue
        second = int(at.timestamp())
        by_second[second].add(track_id)
        first_at.setdefault(second, at)
    moments: list[datetime] = []
    streak_start: int | None = None
    last_over: int | None = None
    reported = False
    for second in sorted(by_second):
        present: set[str] = set()
        for near in range(second - PRESENT_WITHIN, second + PRESENT_WITHIN + 1):
            present |= by_second.get(near, set())
        if len(present) <= limit:
            streak_start, last_over, reported = None, None, False
            continue
        if streak_start is None or last_over is None or second - last_over > PRESENT_WITHIN:
            streak_start, reported = second, False
        last_over = second
        if not reported and second - streak_start >= hold:
            moments.append(first_at[second])
            reported = True
    return moments


def window_rows(db: sqlite3.Connection, camera_id: str, classes: Iterable[str], start: datetime, end: datetime) -> list[tuple[str, datetime, float, float]]:
    """Detections of the classes on a camera in [start, end), as ``exceedances`` takes them."""
    labels = list(classes)
    marks = ",".join("?" for _ in labels)
    rows = db.execute(
        f"SELECT track_id,detected_at,box_x,box_y,box_width,box_height FROM ai_detections WHERE camera_id=? AND label IN ({marks}) AND detected_at>=? AND detected_at<? ORDER BY detected_at",
        (camera_id, *labels, start.isoformat(), end.isoformat())).fetchall()
    result = []
    for row in rows:
        at = datetime.fromisoformat(row["detected_at"].replace("Z", "+00:00"))
        result.append((row["track_id"], at, row["box_x"] + row["box_width"] / 2, row["box_y"] + row["box_height"] / 2))
    return result


def window_for(condition: dict[str, Any], newest: datetime) -> tuple[datetime, datetime]:
    return newest - timedelta(seconds=condition["for_seconds"] + WINDOW_MARGIN + PRESENT_WITHIN), newest + timedelta(seconds=PRESENT_WITHIN + 1)
