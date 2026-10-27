"""Structured detections and tracks for a camera and time range."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from app import privacy
from app.principal import CurrentUser
from app.store import connect
from app.tracks import detection_view, iter_unanalyzed, track_view

router = APIRouter()

COVERAGE_NOTE = "Detections describe analysed frames only; an unanalysed interval is unknown, not empty."


def _range(start: str, end: str) -> tuple[str, str]:
    try:
        start_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if start_time.tzinfo is None or end_time.tzinfo is None or end_time <= start_time:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="start and end must be timezone-aware and form a positive interval") from exc
    return start_time.astimezone(timezone.utc).isoformat(), end_time.astimezone(timezone.utc).isoformat()


def _require_camera(db, camera_id: str) -> None:
    if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="Camera not found")


@router.get("/api/ai/detections")
def list_detections(
    _: CurrentUser,
    camera_id: str = Query(max_length=64),
    start: str = Query(max_length=64),
    end: str = Query(max_length=64),
    label: str | None = Query(default=None, max_length=32),
    track_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, object]:
    start_text, end_text = _range(start, end)
    clauses = ["camera_id=?", "detected_at>=?", "detected_at<?"]
    values: list[object] = [camera_id, start_text, end_text]
    if label:
        clauses.append("label=?")
        values.append(label)
    if track_id:
        clauses.append("track_id=?")
        values.append(track_id)
    with connect() as db:
        _require_camera(db, camera_id)
        rows = db.execute(
            "SELECT * FROM ai_detections WHERE " + " AND ".join(clauses) + " ORDER BY detected_at,id LIMIT ?", (*values, limit)
        ).fetchall()
        unanalyzed = list(iter_unanalyzed(db, camera_id, start_text, end_text))
    return {"camera_id": camera_id, "start": start_text, "end": end_text, "detections": [detection_view(row) for row in rows],
            "truncated": len(rows) == limit, "unanalyzed": unanalyzed, "coverage_note": COVERAGE_NOTE}


@router.get("/api/ai/tracks")
def list_tracks(
    _: CurrentUser,
    camera_id: str = Query(max_length=64),
    start: str = Query(max_length=64),
    end: str = Query(max_length=64),
    label: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, object]:
    start_text, end_text = _range(start, end)
    clauses = ["camera_id=?", "first_seen_at<?", "last_seen_at>=?"]
    values: list[object] = [camera_id, end_text, start_text]
    if label:
        clauses.append("label=?")
        values.append(label)
    with connect() as db:
        _require_camera(db, camera_id)
        rows = db.execute(
            "SELECT * FROM ai_tracks WHERE " + " AND ".join(clauses) + " ORDER BY first_seen_at,id LIMIT ?", (*values, limit)
        ).fetchall()
        unanalyzed = list(iter_unanalyzed(db, camera_id, start_text, end_text))
    return {"camera_id": camera_id, "start": start_text, "end": end_text, "tracks": [track_view(row) for row in rows],
            "truncated": len(rows) == limit, "unanalyzed": unanalyzed, "coverage_note": COVERAGE_NOTE}


@router.get("/api/ai/tracks/{track_id}")
def get_track(track_id: str, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        track = db.execute("SELECT * FROM ai_tracks WHERE id=?", (track_id,)).fetchone()
        if track is None or not user.access.can_see(track["camera_id"]):
            raise HTTPException(status_code=404, detail="Track not found")
        rows = db.execute("SELECT * FROM ai_detections WHERE track_id=? ORDER BY detected_at,id LIMIT 5000", (track_id,)).fetchall()
    return {**track_view(track), "detections": [detection_view(row) for row in rows]}


OVERLAY_LIMIT = 5000
MAX_OVERLAY_WINDOW_SECONDS = 600


def _hidden_by_mask(row: Any, zones: list[dict[str, float]]) -> bool:
    """A box that touches a privacy-masked area is never drawn: its place would show that something is behind the mask."""
    left, top, right, bottom = row["box_x"], row["box_y"], row["box_x"] + row["box_width"], row["box_y"] + row["box_height"]
    return any(left < zone["x"] + zone["width"] and right > zone["x"] and top < zone["y"] + zone["height"] and bottom > zone["y"] for zone in zones)


def _overlay_zones(user: Any, camera_id: str, at: datetime, unmasked: bool) -> list[dict[str, float]]:
    try:
        return privacy.zones_for(user, camera_id, unmasked=unmasked, recorded_at=at)
    except privacy.UnmaskedRefused as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _point(row: Any) -> dict[str, Any]:
    return {"track_id": row["track_id"], "label": row["label"], "confidence": row["confidence"],
            "box": {"x": row["box_x"], "y": row["box_y"], "width": row["box_width"], "height": row["box_height"]}}


@router.get("/api/playback/overlay")
def playback_overlay(
    user: CurrentUser,
    camera_id: str = Query(max_length=64),
    start: str = Query(max_length=64),
    end: str = Query(max_length=64),
    unmasked: Annotated[bool, Query()] = False,
) -> dict[str, object]:
    """The stored detections of one camera for a stretch of playback, one entry for each analysed frame, for drawing boxes over video.

    Only what the AI worker stored is returned, never a live inference. Boxes that touch a privacy-masked area are left out, and what a mask
    hides is never reported, not even as a count."""
    start_text, end_text = _range(start, end)
    start_time, end_time = datetime.fromisoformat(start_text), datetime.fromisoformat(end_text)
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None or not user.access.can_see(camera_id):
            raise HTTPException(status_code=404, detail="Camera not found")
        if (end_time - start_time).total_seconds() > MAX_OVERLAY_WINDOW_SECONDS:
            raise HTTPException(status_code=422, detail=f"Ask for at most {MAX_OVERLAY_WINDOW_SECONDS // 60} minutes at a time")
        zones = _overlay_zones(user, camera_id, start_time, unmasked)
        rows = db.execute("SELECT * FROM ai_detections WHERE camera_id=? AND detected_at>=? AND detected_at<? ORDER BY detected_at,id LIMIT ?",
                          (camera_id, start_text, end_text, OVERLAY_LIMIT)).fetchall()
        unanalyzed = list(iter_unanalyzed(db, camera_id, start_text, end_text))
    frames: list[dict[str, Any]] = []
    for row in rows:
        if zones and _hidden_by_mask(row, zones):
            continue
        if not frames or frames[-1]["at"] != row["detected_at"]:
            frames.append({"at": row["detected_at"], "boxes": []})
        frames[-1]["boxes"].append(_point(row))
    return {"camera_id": camera_id, "start": start_text, "end": end_text, "frames": frames, "truncated": len(rows) == OVERLAY_LIMIT,
            "unanalyzed": unanalyzed, "coverage_note": COVERAGE_NOTE}


@router.get("/api/playback/overlay/tracks/{track_id}")
def playback_overlay_track(track_id: str, user: CurrentUser, unmasked: Annotated[bool, Query()] = False) -> dict[str, object]:
    """The whole stored path of one object (a point for each analysed frame it was in, across short occlusions), for its trail and for
    stepping between its appearances. Points touching a privacy-masked area are left out."""
    with connect() as db:
        track = db.execute("SELECT * FROM ai_tracks WHERE id=?", (track_id,)).fetchone()
        if track is None or not user.access.can_see(track["camera_id"]):
            raise HTTPException(status_code=404, detail="Track not found")
        zones = _overlay_zones(user, track["camera_id"], datetime.fromisoformat(track["first_seen_at"]), unmasked)
        rows = db.execute("SELECT * FROM ai_detections WHERE track_id=? ORDER BY detected_at,id LIMIT ?", (track_id, OVERLAY_LIMIT)).fetchall()
    # Points are in runs: a run ends where the object was in a masked area, so a trail is never drawn across the mask. The mask itself is not
    # reported, nor how many points it hid.
    points: list[dict[str, Any]] = []
    run = 0
    for row in rows:
        if zones and _hidden_by_mask(row, zones):
            if points and points[-1]["run"] == run:
                run += 1
            continue
        points.append({"at": row["detected_at"], "box": _point(row)["box"], "run": run})
    return {"track_id": track_id, "camera_id": track["camera_id"], "label": track["label"], "points": points, "truncated": len(rows) == OVERLAY_LIMIT}
