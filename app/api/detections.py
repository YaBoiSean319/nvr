"""Structured detections and tracks for a camera and time range."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

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
def get_track(track_id: str, _: CurrentUser) -> dict[str, object]:
    with connect() as db:
        track = db.execute("SELECT * FROM ai_tracks WHERE id=?", (track_id,)).fetchone()
        if track is None:
            raise HTTPException(status_code=404, detail="Track not found")
        rows = db.execute("SELECT * FROM ai_detections WHERE track_id=? ORDER BY detected_at,id LIMIT 5000", (track_id,)).fetchall()
    return {**track_view(track), "detections": [detection_view(row) for row in rows]}
