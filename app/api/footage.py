"""Indexed footage, coverage, events, archiving and preservation holds."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from app.api.ai_settings import ai_runtime_model_view, ensure_ai_settings
from app.api.cameras import camera_view
from app.principal import Administrator, CurrentUser, requires_scope
from app.store import connect
from app import track_privacy
from app.health import recording_health
from app.scheduling import effective_policy
from app.preservation import delete_segment as delete_preserved_segment, get_segment_hold, preserve_segment, release_segment_hold

router = APIRouter()


class CameraFootageDeleteRequest(BaseModel):
    confirm_camera_name: str = Field(min_length=1, max_length=120)

    @field_validator("confirm_camera_name")
    @classmethod
    def trim_confirmed_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter the archived camera name to confirm deletion")
        return value


class HoldRequest(BaseModel):
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


@router.get("/api/cameras/{camera_id}/motion-events")
def list_camera_motion_events(
    camera_id: str,
    _: CurrentUser,
) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        rows = db.execute(
            "SELECT id,source,source_topic,source_event_at,received_at,requested_start,requested_end,actual_start,actual_end,pre_roll_complete,status,failure_reason,segment_id "
            "FROM motion_events WHERE camera_id=? ORDER BY received_at DESC LIMIT 200", (camera_id,)
        ).fetchall()
    events = []
    for row in rows:
        item = dict(row)
        item["pre_roll_complete"] = bool(item["pre_roll_complete"])
        item["media_url"] = f"/api/cameras/{camera_id}/segments/{item['segment_id']}/media" if item["segment_id"] else None
        events.append(item)
    return {"events": events}


@router.get("/api/ai/events")
def search_ai_observations(
    user: CurrentUser,
    camera_id: str | None = Query(default=None, max_length=64),
    label: Literal["person", "vehicle"] | None = None,
    start: str | None = Query(default=None, max_length=64),
    end: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    clauses: list[str] = []
    values: list[object] = []
    if start and end:
        try:
            start_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_time = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if start_time.tzinfo is None or end_time.tzinfo is None or end_time <= start_time:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Search range must be timezone-aware and have a positive duration") from exc
    if camera_id:
        with connect() as db:
            if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="Camera not found")
        clauses.append("o.camera_id=?")
        values.append(camera_id)
    if label:
        clauses.append("o.label=?")
        values.append(label)
    if not user.access.unrestricted:
        # Only the cameras this person may see: a search across every camera must not reach the others.
        permitted = sorted(user.access.cameras or ())
        clauses.append("o.camera_id IN (%s)" % ",".join("?" * len(permitted)) if permitted else "0")
        values.extend(permitted)
    for column, raw, operator in (("o.first_seen_at", start, ">="), ("o.first_seen_at", end, "<=")):
        if raw:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
                values.append(parsed.astimezone(timezone.utc).isoformat())
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Search timestamps must include a timezone") from exc
            clauses.append(f"{column}{operator}?")
    with connect() as db:
        # Nothing seen behind a privacy mask is listed (#375), as on the Playback timeline: an observation whose box touches a mask, or cannot be
        # compared with one, is left out. Checked in the query, so the limit never counts what was hidden.
        if camera_id:
            in_scope = [camera_id]
        elif not user.access.unrestricted:
            in_scope = sorted(user.access.cameras or ())
        else:
            in_scope = [row[0] for row in db.execute("SELECT id FROM cameras")]
        visible, visible_values = track_privacy.box_json_visible_sql(track_privacy.zones_by_camera(db, in_scope), "o")
        clauses.append(visible)
        values.extend(visible_values)
        rows = db.execute(
            "SELECT o.*,c.name AS camera_name FROM ai_observations o JOIN cameras c ON c.id=o.camera_id WHERE " + " AND ".join(clauses) + " ORDER BY o.first_seen_at DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
    events = []
    for row in rows:
        item = dict(row)
        item["box"] = json.loads(item.pop("box_json"))
        events.append(item)
    models_used = sorted({(str(item["model_id"]), str(item["model_version"])) for item in events})
    return {"events": events, "model": ai_runtime_model_view(), "models_used": [{"id": model_id, "version": version} for model_id, version in models_used], "coverage_note": "Search results describe analyzed frames only; empty results do not prove the scene was empty."}


@router.get("/api/cameras/{camera_id}/ai/coverage")
def get_ai_detection_coverage(camera_id: str, _: CurrentUser) -> dict[str, object]:
    ensure_ai_settings(camera_id)
    with connect() as db:
        rows = db.execute("SELECT state,starts_at,ends_at,detail FROM ai_coverage WHERE camera_id=? ORDER BY starts_at DESC LIMIT 500", (camera_id,)).fetchall()
        setting = db.execute("SELECT status,last_attempt_at,last_success_at,last_error FROM ai_camera_settings WHERE camera_id=?", (camera_id,)).fetchone()
    return {"status": setting["status"], "last_attempt_at": setting["last_attempt_at"], "last_success_at": setting["last_success_at"], "last_error": setting["last_error"], "intervals": [dict(row) for row in rows], "coverage_note": "A failed or delayed interval is unknown coverage; detections only describe sampled frames."}


@router.delete("/api/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_camera(
    camera_id: str,
    request: Request,
    user: Administrator,
) -> Response:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        camera = db.execute("SELECT archived_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if camera is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if camera["archived_at"]:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        archived_at = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE cameras SET archived_at=?,enabled=0,recording_enabled=0,recording_status='archived',last_error=NULL,updated_at=? WHERE id=?",
            (archived_at, archived_at, camera_id),
        )
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "archive", "camera", camera_id, "{}"))
    # Archive state prevents new ingest after restart and the existing task is now cancelled. If this fails, the durable enabled=0 flag
    # still keeps the recorder supervisor from restarting ingest.
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/cameras/{camera_id}/restore", status_code=status.HTTP_200_OK)
def restore_camera(
    camera_id: str,
    user: Administrator,
) -> dict[str, object]:
    with connect() as db:
        row = db.execute("SELECT archived_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if row["archived_at"] is None:
            raise HTTPException(status_code=409, detail="Camera is not archived")
        db.execute("UPDATE cameras SET archived_at=NULL,enabled=1,recording_status='stopped',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (camera_id,))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "restore", "camera", camera_id, "{}"))
        updated = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    return camera_view(updated)


FOOTAGE_DELETE_BATCH = 200  # Segments removed in one write transaction, and at most this many files open at once

_ACTIVE_HOLD = ("SELECT 1 FROM recording_holds WHERE camera_id=? AND released_at IS NULL AND (expires_at IS NULL OR expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) LIMIT 1")
_ACTIVE_EXPORT = ("SELECT 1 FROM export_sources s JOIN recording_segments r ON r.id=s.segment_id "
                  "JOIN exports e ON e.id=s.export_id WHERE r.camera_id=? AND e.status IN ('queued','running') LIMIT 1")


def _refuse_protected_footage(db: sqlite3.Connection, camera_id: str) -> None:
    if db.execute(_ACTIVE_HOLD, (camera_id,)).fetchone():
        raise HTTPException(status_code=409, detail="Archived camera has preserved footage; release its holds before deleting footage")
    if db.execute(_ACTIVE_EXPORT, (camera_id,)).fetchone():
        raise HTTPException(status_code=409, detail="Archived camera footage is in an active export")


@router.delete("/api/cameras/{camera_id}/footage", status_code=status.HTTP_204_NO_CONTENT)
def delete_archived_camera_footage(
    camera_id: str,
    payload: CameraFootageDeleteRequest,
    user: Administrator,
) -> Response:
    """Delete every recording of an archived camera.

    Everything that can refuse is looked at first, before anything is removed: a hold, a queued or running export, storage that is not
    there, a file being played. Then the footage goes ``FOOTAGE_DELETE_BATCH`` segments at a time, each batch its own short write
    transaction, so recording and everything else keep writing in between, and no more than a batch of files is ever open (#361).
    One transaction for a camera with a month of footage held the write lock for most of an hour and opened every file at once.
    A delete that stops part-way (a file starts being played, the server restarts) has removed whole batches; running it again finishes."""
    from app import tiering
    from app.retention import playback_is_leased
    from app.storage import recording_file_path, remove_segment_files

    columns = "id,relative_path,tier_id,tier_key,tier_sha256"
    with connect() as db:
        camera = db.execute("SELECT name,archived_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if camera is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if camera["archived_at"] is None:
            raise HTTPException(status_code=409, detail="Archive the camera before deleting its footage")
        if payload.confirm_camera_name != camera["name"]:
            raise HTTPException(status_code=422, detail="Camera name confirmation does not match")
        _refuse_protected_footage(db, camera_id)
        segments = db.execute(f"SELECT {columns} FROM recording_segments WHERE camera_id=? ORDER BY id", (camera_id,)).fetchall()
    try:
        for row in segments:
            if row["tier_id"]:
                continue
            # Hot storage that is not there is refused here, as before; and a file being played, one file open at a time.
            if playback_is_leased(recording_file_path(row["relative_path"])):
                raise HTTPException(status_code=409, detail="Archived camera footage is currently being played")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Assigned recording storage is unavailable") from exc

    deleted = 0
    for offset in range(0, len(segments), FOOTAGE_DELETE_BATCH):
        batch = segments[offset:offset + FOOTAGE_DELETE_BATCH]
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _refuse_protected_footage(db, camera_id)  # A hold or an export made since the check above still stops what is left
            try:
                removed = remove_segment_files(batch)
            except OSError as exc:  # Its files are closed again by now; what is left can be deleted once the storage is back
                raise HTTPException(status_code=503, detail="Assigned recording storage is unavailable") from exc
            if not removed:
                raise HTTPException(status_code=409, detail="Archived camera footage is currently being played")
            marks = ",".join("?" * len(batch))
            db.execute(f"DELETE FROM recording_segments WHERE id IN ({marks})", [row["id"] for row in batch])
        tiering.discard_objects(batch)  # Copies in object stores go once the rows are gone, outside the write lock
        deleted += len(batch)
    # Including detections between motion clips: nothing of this camera's recorded activity remains. In batches too: a track takes its detections with it.
    while True:
        with connect() as db:
            tracks = [row[0] for row in db.execute("SELECT id FROM ai_tracks WHERE camera_id=? LIMIT ?", (camera_id, FOOTAGE_DELETE_BATCH))]
            if tracks:
                db.execute(f"DELETE FROM ai_tracks WHERE id IN ({','.join('?' * len(tracks))})", tracks)
        if len(tracks) < FOOTAGE_DELETE_BATCH:
            break
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "delete_camera_footage", "camera", camera_id,
                    json.dumps({"segments_deleted": deleted}, separators=(",", ":"))))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/cameras/{camera_id}/segments")
def list_camera_segments(
    camera_id: str,
    _: CurrentUser,
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
) -> dict[str, object]:
    with connect() as db:
        camera = db.execute("SELECT id, recording_status FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if not camera:
            raise HTTPException(status_code=404, detail="Camera not found")
        if (start is None) != (end is None):
            raise HTTPException(status_code=422, detail="Provide both from and to, or neither")
        if start is None or end is None:
            rows = db.execute(
                "SELECT s.id,s.starts_at,s.ends_at,s.size_bytes,s.unavailable_at,s.unavailable_reason,(SELECT h.id FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) ORDER BY h.created_at DESC LIMIT 1) AS hold_id,(SELECT h.expires_at FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) ORDER BY h.created_at DESC LIMIT 1) AS hold_expires_at FROM recording_segments s WHERE camera_id=? ORDER BY starts_at DESC LIMIT 500",
                (camera_id,),
            ).fetchall()
        else:
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                raise HTTPException(status_code=422, detail="from and to must be timezone-aware and form a positive interval")
            rows = db.execute(
                "SELECT s.id,s.starts_at,s.ends_at,s.size_bytes,s.unavailable_at,s.unavailable_reason,(SELECT h.id FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) ORDER BY h.created_at DESC LIMIT 1) AS hold_id,(SELECT h.expires_at FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) ORDER BY h.created_at DESC LIMIT 1) AS hold_expires_at FROM recording_segments s "
                "WHERE camera_id=? AND starts_at<? AND ends_at>? ORDER BY starts_at",
                (camera_id, end.astimezone(timezone.utc).isoformat(), start.astimezone(timezone.utc).isoformat()),
            ).fetchall()
    segments = [
        {"id": row["id"], "starts_at": row["starts_at"], "ends_at": row["ends_at"], "size_bytes": row["size_bytes"], "preserved":row["hold_id"] is not None,"hold_expires_at":row["hold_expires_at"],
         "available": row["unavailable_at"] is None, "unavailable_reason": row["unavailable_reason"],
         "media_url": f"/api/cameras/{camera_id}/segments/{row['id']}/media" if row["unavailable_at"] is None else None}
        for row in rows
    ]
    return {
        "camera_id": camera_id,
        "recording_status": camera["recording_status"],
        "availability": "available" if any(segment["available"] for segment in segments) else "unavailable" if segments else "none",
        "reason": None if segments else "No indexed footage covers this interval",
        "segments": segments,
    }


@router.get("/api/cameras/{camera_id}/coverage")
def list_camera_coverage(
    camera_id: str,
    _: CurrentUser,
) -> dict[str, object]:
    with connect() as db:
        camera = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if not camera:
            raise HTTPException(status_code=404, detail="Camera not found")
        policy = effective_policy(db, camera_id, datetime.now(timezone.utc))
        health = recording_health(
            enabled=bool(camera["enabled"]), recording_enabled=bool(camera["recording_enabled"]),
            mode=camera["recording_mode"], effective_mode=str(policy["mode"]),
            recording_status=camera["recording_status"], motion_source_status=camera["motion_source_status"],
            last_video_at=camera["last_video_at"], last_footage_write_at=camera["last_footage_write_at"],
        )
        rows = db.execute(
            "SELECT coverage_kind,reason,reason_code,starts_at,ends_at FROM coverage_intervals WHERE camera_id=? ORDER BY starts_at DESC LIMIT 200",
            (camera_id,),
        ).fetchall()
        backfilled = [dict(row) for row in db.execute(
            "SELECT id,starts_at,ends_at,size_bytes FROM recording_segments WHERE camera_id=? AND source='backfill' AND unavailable_at IS NULL ORDER BY starts_at DESC LIMIT 200", (camera_id,))]
    return {"camera_id": camera_id, "current": health, "intervals": [dict(row) for row in reversed(rows)], "backfilled": list(reversed(backfilled))}


@router.get("/api/cameras/{camera_id}/segments/{segment_id}/hold")
def segment_hold_status(camera_id: str, segment_id: int, _: CurrentUser) -> dict[str, object]:
    try:
        return get_segment_hold(camera_id,segment_id)
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc


@router.post("/api/cameras/{camera_id}/segments/{segment_id}/hold",status_code=status.HTTP_201_CREATED)
@requires_scope("export")
def preserve_camera_segment(camera_id: str, segment_id: int, payload: HoldRequest, user: CurrentUser) -> dict[str, object]:
    if payload.expires_in_days is not None and user["role"]!="administrator":
        raise HTTPException(status_code=403,detail="Only administrators can set a hold expiry")
    try:
        return preserve_segment(camera_id,segment_id,int(user["id"]),payload.expires_in_days)
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=410,detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503,detail="Assigned storage volume is unavailable") from exc


@router.delete("/api/cameras/{camera_id}/segments/{segment_id}/hold",status_code=status.HTTP_204_NO_CONTENT)
def release_camera_segment(camera_id: str, segment_id: int, user: Administrator) -> Response:
    try:
        release_segment_hold(camera_id,segment_id,int(user["id"]))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    return Response(status_code=204)


@router.delete("/api/cameras/{camera_id}/segments/{segment_id}",status_code=status.HTTP_204_NO_CONTENT)
def delete_camera_segment(camera_id: str, segment_id: int, user: Administrator) -> Response:
    try:
        delete_preserved_segment(camera_id,segment_id,int(user["id"]))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    except BlockingIOError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503,detail="Assigned storage volume is unavailable") from exc
    return Response(status_code=204)
