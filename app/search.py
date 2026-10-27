"""Attribute search across cameras: find tracks by class, colours, vehicle type, direction, zone, cameras and time.

A search reads the attributes the AI worker stored with each track (see ``attributes``). Every answer carries the coverage of
the range it searched: for each camera, how much of the time was analysed and the intervals that were not, whether the AI was
delayed, failed or interrupted, or was not running at all. "No match" in time that was not analysed means nothing was looked at,
so those intervals are listed with the results, never left for the reader to infer.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .attributes import DIRECTIONS, PALETTE, VEHICLE_TYPES, attribute_model_available, zone_cells

MAX_RANGE = timedelta(days=92)
MAX_LIMIT = 200
LABELS = ("person", "vehicle")
MAX_INTERVALS_PER_CAMERA = 20
ZONE_PRESETS = {
    "left": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0}, "right": {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0},
    "top": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 0.5}, "bottom": {"x": 0.0, "y": 0.5, "width": 1.0, "height": 0.5},
    "centre": {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5},
}


def options() -> dict[str, Any]:
    return {"labels": list(LABELS), "colours": list(PALETTE), "vehicle_types": list(VEHICLE_TYPES), "directions": list(DIRECTIONS),
            "vehicle_type_available": attribute_model_available(), "similar_available": True, "zones": ZONE_PRESETS, "max_range_days": MAX_RANGE.days, "max_limit": MAX_LIMIT}


def parse_zone(value: str) -> dict[str, float]:
    """A zone as a preset name (left, right, top, bottom, centre) or ``x,y,width,height`` as fractions of the frame; ValueError otherwise."""
    if value in ZONE_PRESETS:
        return dict(ZONE_PRESETS[value])
    try:
        x, y, width, height = (float(part) for part in value.split(","))
    except ValueError:
        raise ValueError("A zone is one of " + ", ".join(ZONE_PRESETS) + " or x,y,width,height as fractions of the frame") from None
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 - x + 1e-9 and 0 < height <= 1 - y + 1e-9):
        raise ValueError("A zone must lie inside the frame (x, y from 0, width and height above 0)")
    return {"x": x, "y": y, "width": width, "height": height}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def find_tracks(db: sqlite3.Connection, camera_ids: list[str], start: datetime, end: datetime, *, label: str | None = None, upper_colour: str | None = None,
                lower_colour: str | None = None, vehicle_colour: str | None = None, vehicle_type: str | None = None, direction: str | None = None,
                zone: dict[str, float] | None = None, order: str = "desc", limit: int = 60) -> tuple[list[dict[str, Any]], bool]:
    """The tracks in range matching every filter given, newest first unless ``order`` is ``asc``, and whether more matched than ``limit``."""
    if not camera_ids:
        return [], False
    clauses = [f"t.camera_id IN ({','.join('?' * len(camera_ids))})", "t.last_seen_at>=?", "t.first_seen_at<?"]
    values: list[Any] = [*camera_ids, _iso(start), _iso(end)]
    for column, value in (("label", label), ("upper_colour", upper_colour), ("lower_colour", lower_colour), ("vehicle_colour", vehicle_colour),
                          ("vehicle_type", vehicle_type), ("direction", direction)):
        if value:
            clauses.append(f"t.{column}=?")
            values.append(value)
    if zone:
        # The grid prefilter is cheap and never rules out a track that did pass through; the exact check on the survivors settles it.
        clauses.append("(t.zone_mask & ?)!=0")
        values.append(zone_cells(zone))
        clauses.append("EXISTS (SELECT 1 FROM ai_detections d WHERE d.track_id=t.id AND d.box_x+d.box_width/2 BETWEEN ? AND ? AND d.box_y+d.box_height/2 BETWEEN ? AND ?)")
        values.extend([zone["x"], zone["x"] + zone["width"], zone["y"], zone["y"] + zone["height"]])
    direction_sql = "DESC" if order == "desc" else "ASC"
    rows = db.execute(f"{_TRACK_COLUMNS} WHERE {' AND '.join(clauses)} ORDER BY t.first_seen_at {direction_sql},t.id {direction_sql} LIMIT ?", (*values, limit + 1)).fetchall()
    return [_result(row) for row in rows[:limit]], len(rows) > limit


def _result(row: sqlite3.Row) -> dict[str, Any]:
    box = None
    if row["first_box"]:
        x, y, width, height = (float(part) for part in row["first_box"].split(","))
        box = {"x": x, "y": y, "width": width, "height": height}
    return {
        "track_id": row["id"], "camera_id": row["camera_id"], "camera_name": row["camera_name"], "label": row["label"],
        "first_seen_at": row["first_seen_at"], "last_seen_at": row["last_seen_at"], "detection_count": row["detection_count"], "confidence": row["max_confidence"],
        "attributes": {key: row[key] for key in ("upper_colour", "lower_colour", "vehicle_colour", "vehicle_type", "direction") if row[key]},
        "thumbnail_url": f"/api/ai/tracks/{row['id']}/thumbnail" if row["has_thumbnail"] else None, "box": box,
    }


_TRACK_COLUMNS = ("SELECT t.*,c.name AS camera_name,EXISTS(SELECT 1 FROM track_keyframes k WHERE k.track_id=t.id) AS has_thumbnail,"
                  "(SELECT box_x||','||box_y||','||box_width||','||box_height FROM ai_detections d WHERE d.track_id=t.id ORDER BY d.detected_at,d.id LIMIT 1) AS first_box "
                  "FROM ai_tracks t JOIN cameras c ON c.id=t.camera_id")


def tracks_by_ids(db: sqlite3.Connection, track_ids: list[str]) -> list[dict[str, Any]]:
    """The search-result view of these tracks, in the order given (a track that no longer exists is left out)."""
    if not track_ids:
        return []
    found = {row["id"]: _result(row) for row in db.execute(f"{_TRACK_COLUMNS} WHERE t.id IN ({','.join('?' * len(track_ids))})", track_ids)}
    return [found[track_id] for track_id in track_ids if track_id in found]


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for begin, finish in sorted(intervals):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
        else:
            merged.append((begin, finish))
    return merged


def coverage(db: sqlite3.Connection, cameras: dict[str, str], start: datetime, end: datetime) -> dict[str, Any]:
    """How much of the range each camera was analysed for, and the intervals that were not (with why), across the cameras searched.

    A camera's time is unanalysed where its AI coverage says so (delayed, failed, interrupted, not analysed) and wherever there is no
    coverage at all: the AI was off, or never turned on, then.
    """
    total = max(0.0, (end - start).total_seconds())
    if not cameras or total == 0:
        return {"range_seconds": total, "searched_cameras": len(cameras), "analysed_percent": None, "fully_analysed": not cameras, "cameras": []}
    rows: dict[str, list[sqlite3.Row]] = {camera_id: [] for camera_id in cameras}
    ids = list(cameras)
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        for row in db.execute(f"SELECT camera_id,state,starts_at,ends_at,detail FROM ai_coverage WHERE camera_id IN ({','.join('?' * len(part))}) "
                              "AND starts_at<? AND (ends_at IS NULL OR ends_at>?) ORDER BY starts_at", (*part, _iso(end), _iso(start))):
            rows[row["camera_id"]].append(row)
    report = []
    analysed_total = 0.0
    for camera_id, name in cameras.items():
        analysed: list[tuple[datetime, datetime]] = []
        covered: list[tuple[datetime, datetime]] = []
        segments: list[dict[str, Any]] = []
        for row in rows[camera_id]:
            begin, finish = max(start, _parse(row["starts_at"])), min(end, _parse(row["ends_at"]) if row["ends_at"] else end)
            if finish <= begin:
                continue
            covered.append((begin, finish))
            if row["state"] == "analyzed":
                analysed.append((begin, finish))
            else:
                segments.append({"state": row["state"], "starts_at": _iso(begin), "ends_at": _iso(finish), "detail": row["detail"] or "", "seconds": (finish - begin).total_seconds()})
        cursor = start
        for begin, finish in _merge(covered):
            if begin > cursor:
                segments.append({"state": "not_running", "starts_at": _iso(cursor), "ends_at": _iso(begin), "detail": "AI analysis was not running", "seconds": (begin - cursor).total_seconds()})
            cursor = max(cursor, finish)
        if cursor < end:
            segments.append({"state": "not_running", "starts_at": _iso(cursor), "ends_at": _iso(end), "detail": "AI analysis was not running", "seconds": (end - cursor).total_seconds()})
        seconds = sum((finish - begin).total_seconds() for begin, finish in _merge(analysed))
        analysed_total += seconds
        if seconds < total:
            segments.sort(key=lambda item: item["starts_at"])
            report.append({"camera_id": camera_id, "camera_name": name, "analysed_seconds": round(seconds, 1), "unanalysed_seconds": round(total - seconds, 1),
                           "intervals": segments[:MAX_INTERVALS_PER_CAMERA], "more_intervals": max(0, len(segments) - MAX_INTERVALS_PER_CAMERA)})
    report.sort(key=lambda item: (-item["unanalysed_seconds"], item["camera_name"].lower()))
    percent = round(100 * analysed_total / (total * len(cameras)), 1)
    return {"range_seconds": total, "searched_cameras": len(cameras), "analysed_percent": percent, "fully_analysed": not report, "cameras": report}
