"""One privacy-aware projection of detections and tracks (#296).

The playback overlay never reports a box that touches a privacy mask, nor how many it hid. The structured endpoints (raw detections, tracks, search,
find-similar) used to return the same detections to anyone who asked another way. Everything that exposes them now goes through here:

* a detection whose box touches a mask is left out, by the same rule as the overlay (any overlap), and in SQL, so ``limit`` and ``truncated`` never
  depend on what was hidden;
* a track with no permitted detection is not there at all (a 404 for its detail, absent from lists), indistinguishable from one that never existed;
* a partly hidden ("partial") track shows only its permitted detections, with the count, first and last seen and best confidence worked out from those
  alone, and with its attributes withheld (they may have come from frames the mask hides);
* where results are ranked by appearance (the natural-language search, find-similar) a partial track is left out altogether: its picture is the
  unmasked frame, so its place in a ranking would say what is behind the mask.

The masks used are the ones that apply to the camera's oldest footage (``EARLIEST``), the widest set, so a track is never shown because a mask was
later narrowed or because the camera started masking its own picture. Unmasked viewing is a playback matter (the overlay, with an administrator's
stated reason); nothing here takes ``unmasked``.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from . import privacy

EARLIEST = datetime(1970, 1, 1, tzinfo=timezone.utc)
Zones = dict[str, list[dict[str, float]]]


def zones_by_camera(db: sqlite3.Connection, camera_ids: Iterable[str]) -> Zones:
    """The masks of these cameras (only cameras that have any)."""
    found: Zones = {}
    for camera_id in dict.fromkeys(camera_ids):
        zones = privacy.server_zones(db, camera_id, EARLIEST)
        if zones:
            found[camera_id] = zones
    return found


def box_touches(box: dict[str, float], zones: list[dict[str, float]]) -> bool:
    """Whether a box (x, y, width, height as fractions of the picture) overlaps any masked area: the overlay's rule."""
    left, top, right, bottom = box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]
    return any(left < z["x"] + z["width"] and right > z["x"] and top < z["y"] + z["height"] and bottom > z["y"] for z in zones)


def detection_visible_sql(zones: Zones, alias: str = "d") -> tuple[str, list[Any]]:
    """SQL that is true for a detection a person may see: its camera has no mask, or its box touches none of them."""
    if not zones:
        return "1", []
    parts = [f"{alias}.camera_id NOT IN ({','.join('?' * len(zones))})"]
    params: list[Any] = list(zones)
    for camera_id, areas in zones.items():
        overlap = " OR ".join(f"({alias}.box_x < ? AND {alias}.box_x + {alias}.box_width > ? AND {alias}.box_y < ? AND {alias}.box_y + {alias}.box_height > ?)" for _ in areas)
        parts.append(f"({alias}.camera_id=? AND NOT ({overlap}))")
        params.append(camera_id)
        for z in areas:
            params.extend([z["x"] + z["width"], z["x"], z["y"] + z["height"], z["y"]])
    return "(" + " OR ".join(parts) + ")", params


_BOX_KEYS = ("x", "y", "width", "height")


def box_usable(box: Any) -> bool:
    """Whether a stored box can be compared with a mask: an object with all four numbers."""
    return isinstance(box, dict) and all(isinstance(box.get(key), (int, float)) and not isinstance(box.get(key), bool) for key in _BOX_KEYS)


def box_json_visible_sql(zones: Zones, alias: str = "r", column: str = "box_json") -> tuple[str, list[Any]]:
    """SQL that is true for a row whose box, kept as JSON text in ``column``, a person may see: its camera has no mask, or the box touches none.

    A box that is not valid JSON, or lacks any of its four numbers, cannot be compared with a mask, so on a camera that has one it is not shown (fail
    closed). The checks are nested ``CASE``s because SQLite evaluates those lazily: ``json_extract`` on text that is not JSON is an error."""
    if not zones:
        return "1", []
    col = f"{alias}.{column}"

    def number(key: str) -> str:
        return f"json_extract({col},'$.{key}')"

    usable = f"json_type({col})='object' AND " + " AND ".join(f"json_type({col},'$.{key}') IN ('integer','real')" for key in _BOX_KEYS)
    parts = [f"{alias}.camera_id NOT IN ({','.join('?' * len(zones))})"]
    params: list[Any] = list(zones)
    for camera_id, areas in zones.items():
        overlap = " OR ".join(f"({number('x')} < ? AND {number('x')} + {number('width')} > ? AND {number('y')} < ? AND {number('y')} + {number('height')} > ?)" for _ in areas)
        parts.append(f"({alias}.camera_id=? AND CASE WHEN json_valid({col}) THEN (CASE WHEN {usable} THEN NOT ({overlap}) ELSE 0 END) ELSE 0 END)")
        params.append(camera_id)
        for z in areas:
            params.extend([z["x"] + z["width"], z["x"], z["y"] + z["height"], z["y"]])
    return "(" + " OR ".join(parts) + ")", params


def track_visible_sql(zones: Zones, *, whole: bool = False, alias: str = "t", window: tuple[str, str] | None = None) -> tuple[str, list[Any]]:
    """SQL for ``ai_tracks`` rows a person may see: a track on an unmasked camera, or on a masked one with a permitted detection.

    ``whole`` is for results ranked or filtered by something that comes from the whole track (appearance, colours, direction): a track that has any
    detection touching a mask is then not shown at all. ``window`` (start, end as ISO text) asks for a permitted detection *inside* it, so a track whose
    only activity in the window was hidden is not listed for it."""
    if not zones:
        return "1", []
    visible, params = detection_visible_sql(zones, "d")
    plain = f"{alias}.camera_id NOT IN ({','.join('?' * len(zones))})"
    if whole:
        return f"({plain} OR NOT EXISTS (SELECT 1 FROM ai_detections d WHERE d.track_id={alias}.id AND NOT {visible}))", [*zones, *params]
    inside = " AND d.detected_at>=? AND d.detected_at<?" if window else ""
    return (f"({plain} OR EXISTS (SELECT 1 FROM ai_detections d WHERE d.track_id={alias}.id AND {visible}{inside}))",
            [*zones, *params, *(window or ())])


def whole_track_visible(db: sqlite3.Connection, track_id: str, camera_id: str) -> bool:
    """Whether a track may be used as the source of an appearance comparison: none of its detections touches a mask."""
    zones = zones_by_camera(db, [camera_id])
    if not zones:
        return True
    visible, params = track_visible_sql(zones, whole=True, alias="t")
    return db.execute(f"SELECT 1 FROM ai_tracks t WHERE t.id=? AND {visible}", (track_id, *params)).fetchone() is not None


def _stats(db: sqlite3.Connection, track_ids: list[str], zones: Zones) -> dict[str, dict[str, Any]]:
    visible, params = detection_visible_sql(zones, "d")
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(track_ids), 500):
        chunk = track_ids[start:start + 500]
        rows = db.execute(
            "SELECT track_id,SUM(hidden) AS hidden,SUM(1-hidden) AS shown,MIN(CASE WHEN hidden=0 THEN detected_at END) AS first_at,"
            "MAX(CASE WHEN hidden=0 THEN detected_at END) AS last_at,MAX(CASE WHEN hidden=0 THEN confidence END) AS best "
            f"FROM (SELECT d.track_id,d.detected_at,d.confidence,CASE WHEN {visible} THEN 0 ELSE 1 END AS hidden FROM ai_detections d "
            f"WHERE d.track_id IN ({','.join('?' * len(chunk))})) GROUP BY track_id", (*params, *chunk)).fetchall()
        found.update({row["track_id"]: dict(row) for row in rows})
    return found


def _first_visible_box(db: sqlite3.Connection, track_id: str, zones: Zones) -> dict[str, float] | None:
    visible, params = detection_visible_sql(zones, "d")
    row = db.execute(f"SELECT d.box_x,d.box_y,d.box_width,d.box_height FROM ai_detections d WHERE d.track_id=? AND {visible} ORDER BY d.detected_at,d.id LIMIT 1",
                     (track_id, *params)).fetchone()
    return None if row is None else {"x": row["box_x"], "y": row["box_y"], "width": row["box_width"], "height": row["box_height"]}


def project(db: sqlite3.Connection, views: list[dict[str, Any]], zones: Zones, *, id_key: str = "id") -> list[dict[str, Any]]:
    """The track views a person may see, partial ones reduced to what is permitted. A view needs ``camera_id``, ``first_seen_at``, ``last_seen_at``,
    ``detection_count``, a confidence (``max_confidence`` or ``confidence``) and ``attributes``; one with a ``box`` has it replaced by its first permitted box."""
    masked = [view[id_key] for view in views if view["camera_id"] in zones]
    stats = _stats(db, masked, zones) if masked else {}
    kept: list[dict[str, Any]] = []
    for view in views:
        if view["camera_id"] not in zones:
            kept.append(view)
            continue
        seen = stats.get(view[id_key])
        if not seen or not seen["shown"]:
            continue
        if seen["hidden"]:
            view = {**view, "detection_count": int(seen["shown"]), "first_seen_at": seen["first_at"], "last_seen_at": seen["last_at"], "attributes": {}}
            view["max_confidence" if "max_confidence" in view else "confidence"] = seen["best"]
            if "box" in view:
                view["box"] = _first_visible_box(db, view[id_key], zones)
        kept.append(view)
    return kept
