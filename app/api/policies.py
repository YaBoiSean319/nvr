"""Camera groups, membership, and the schedule, retention and storage policies they carry."""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
import json
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from app.principal import Administrator, CurrentUser
from app.store import connect
from app.scheduling import effective_policy, validate_schedule
from app.retention import coverage_summary, effective_retention
from app.storage import assign_group_volume, configured_volumes, refresh_registry, storage_status
from app.incidents import sync_storage_incidents
from app.request_validation import normalize_required_group_name

router = APIRouter()


CameraGroupName = Annotated[str, StringConstraints(min_length=1, max_length=80), AfterValidator(normalize_required_group_name)]


class CameraGroupCreateRequest(BaseModel):
    name: CameraGroupName


class CameraGroupUpdateRequest(BaseModel):
    name: CameraGroupName


class CameraGroupMembershipRequest(BaseModel):
    camera_ids: list[str] = Field(max_length=5000)


class RetentionRequest(BaseModel):
    retention_days: int = Field(ge=1, le=3650)


class StorageAssignmentRequest(BaseModel):
    volume_id: str | None = Field(default=None, max_length=64)


class WeeklyScheduleInterval(BaseModel):
    days: list[Annotated[int, Field(ge=0, le=6)]] = Field(min_length=1, max_length=7)
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    mode: Literal["motion", "continuous", "disabled"]


class GroupScheduleRequest(BaseModel):
    timezone: str = Field(min_length=1, max_length=128)
    intervals: list[WeeklyScheduleInterval] = Field(max_length=200)

    @model_validator(mode="after")
    def validate_intervals(self) -> "GroupScheduleRequest":
        try:
            validate_schedule(self.timezone, [interval.model_dump() for interval in self.intervals])
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


class CameraScheduleRequest(GroupScheduleRequest):
    timezone: str = "UTC"
    intervals: list[WeeklyScheduleInterval] = Field(default_factory=list, max_length=200)
    inherit_group: bool = False

    @model_validator(mode="after")
    def require_policy_or_inheritance(self) -> "CameraScheduleRequest":
        if not self.inherit_group and not self.timezone.strip():
            raise ValueError("Choose a timezone or inherit the assigned camera group's schedule")
        return self


@router.get("/api/camera-groups")
def list_camera_groups(_: CurrentUser) -> dict[str, object]:
    with connect() as db:
        groups = db.execute("SELECT id,name FROM camera_groups ORDER BY name COLLATE NOCASE").fetchall()
        return {"groups": [_camera_group_view(db, group) for group in groups]}


def _camera_group_view(db: sqlite3.Connection, group: sqlite3.Row) -> dict[str, object]:
    members = db.execute(
        "SELECT id FROM cameras WHERE group_id=? "
        "UNION SELECT camera_id FROM camera_group_memberships WHERE group_id=?",
        (group["id"], group["id"]),
    ).fetchall()
    camera_ids = [row["id"] for row in members]
    return {"id": group["id"], "name": group["name"], "camera_ids": camera_ids, "camera_count": len(camera_ids)}


@router.get("/api/camera-groups/{group_id}/members")
def list_camera_group_members(
    group_id: str,
    _: CurrentUser,
) -> dict[str, object]:
    with connect() as db:
        group = db.execute("SELECT id,name FROM camera_groups WHERE id=?", (group_id,)).fetchone()
        if group is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        return _camera_group_view(db, group)


@router.post("/api/camera-groups", status_code=status.HTTP_201_CREATED)
def create_camera_group(
    payload: CameraGroupCreateRequest,
    _: Administrator,
) -> dict[str, str]:
    name = payload.name
    group_id = str(uuid.uuid4())
    with connect() as db:
        try:
            db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)", (group_id, name))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="A camera group with that name already exists") from exc
    return {"id": group_id, "name": name}


@router.patch("/api/camera-groups/{group_id}")
def update_camera_group(
    group_id: str,
    payload: CameraGroupUpdateRequest,
    _: Administrator,
) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        try:
            db.execute("UPDATE camera_groups SET name=? WHERE id=?", (payload.name, group_id))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="A camera group with that name already exists") from exc
        group = db.execute("SELECT id,name FROM camera_groups WHERE id=?", (group_id,)).fetchone()
        return _camera_group_view(db, group)


@router.delete("/api/camera-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_camera_group(
    group_id: str,
    _: Administrator,
) -> Response:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        if db.execute("SELECT 1 FROM cameras WHERE group_id=? LIMIT 1", (group_id,)).fetchone() is not None:
            raise HTTPException(status_code=409, detail="Reassign cameras from this policy group before deleting it")
        db.execute("DELETE FROM recording_policy_overrides WHERE scope_type='group' AND scope_id=?", (group_id,))
        db.execute("DELETE FROM retention_policy_overrides WHERE scope_type='group' AND scope_id=?", (group_id,))
        db.execute("DELETE FROM camera_groups WHERE id=?", (group_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/api/camera-groups/{group_id}/members")
def replace_camera_group_members(
    group_id: str,
    payload: CameraGroupMembershipRequest,
    _: Administrator,
) -> dict[str, object]:
    camera_ids = list(dict.fromkeys(payload.camera_ids))
    with connect() as db:
        group = db.execute("SELECT id,name FROM camera_groups WHERE id=?", (group_id,)).fetchone()
        if group is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        if camera_ids:
            placeholders = ",".join("?" for _ in camera_ids)
            found = db.execute(f"SELECT id FROM cameras WHERE id IN ({placeholders})", camera_ids).fetchall()
            if len(found) != len(camera_ids):
                raise HTTPException(status_code=422, detail="One or more selected cameras do not exist")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM camera_group_memberships WHERE group_id=?", (group_id,))
        db.executemany(
            "INSERT INTO camera_group_memberships(group_id,camera_id) VALUES(?,?)",
            [(group_id, camera_id) for camera_id in camera_ids],
        )
        return _camera_group_view(db, group)


@router.get("/api/camera-groups/{group_id}/retention")
def get_group_retention(group_id: str, _: CurrentUser) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        row = db.execute("SELECT retention_days,updated_at FROM retention_policy_overrides WHERE scope_type='group' AND scope_id=?", (group_id,)).fetchone()
    return {"group_id": group_id, "configured": row is not None, "retention_days": int(row["retention_days"]) if row else 30, "policy_source": "group" if row else "default", "updated_at": row["updated_at"] if row else None}


@router.put("/api/camera-groups/{group_id}/retention")
def set_group_retention(group_id: str, payload: RetentionRequest, _: Administrator) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('group',?,?) ON CONFLICT(scope_type,scope_id) DO UPDATE SET retention_days=excluded.retention_days,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')", (group_id,payload.retention_days))
    return get_group_retention(group_id, _=None)


@router.delete("/api/camera-groups/{group_id}/retention", status_code=status.HTTP_204_NO_CONTENT)
def delete_group_retention(group_id: str, _: Administrator) -> Response:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        db.execute("DELETE FROM retention_policy_overrides WHERE scope_type='group' AND scope_id=?", (group_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/cameras/{camera_id}/retention")
def get_camera_retention(camera_id: str, _: CurrentUser) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        days, source = effective_retention(db, camera_id)
        summary = coverage_summary(db, camera_id)
        local = db.execute("SELECT 1 FROM retention_policy_overrides WHERE scope_type='camera' AND scope_id=?", (camera_id,)).fetchone()
    return {"camera_id": camera_id, "inherits_group": local is None, **summary, "policy_source": source}


@router.put("/api/cameras/{camera_id}/retention")
def set_camera_retention(camera_id: str, payload: RetentionRequest, _: Administrator) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,?) ON CONFLICT(scope_type,scope_id) DO UPDATE SET retention_days=excluded.retention_days,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')", (camera_id,payload.retention_days))
    return get_camera_retention(camera_id, _=None)


@router.delete("/api/cameras/{camera_id}/retention", status_code=status.HTTP_204_NO_CONTENT)
def delete_camera_retention(camera_id: str, _: Administrator) -> Response:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        db.execute("DELETE FROM retention_policy_overrides WHERE scope_type='camera' AND scope_id=?", (camera_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/storage")
def get_storage_status(_: Administrator) -> dict[str, object]:
    refresh_registry()
    result = storage_status()
    with connect() as db:
        sync_storage_incidents(db, result["volumes"])
    return result


@router.get("/api/camera-groups/{group_id}/storage-volume")
def get_group_storage_volume(group_id: str, _: Administrator) -> dict[str, object]:
    with connect() as db:
        row=db.execute("SELECT id,name,storage_volume_id FROM camera_groups WHERE id=?",(group_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404,detail="Camera group not found")
    return {"group_id":group_id,"volume_id":row["storage_volume_id"] or "default","configured_volumes":list(configured_volumes())}


@router.put("/api/camera-groups/{group_id}/storage-volume")
async def set_group_storage_volume(group_id: str, payload: StorageAssignmentRequest, request: Request, _: Administrator) -> dict[str, object]:
    try:
        assign_group_volume(group_id,payload.volume_id)
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc
    with connect() as db:
        camera_ids=[row["id"] for row in db.execute("SELECT id FROM cameras WHERE group_id=?",(group_id,)).fetchall()]
    await asyncio.gather(*(request.app.state.recorders.sync_camera(camera_id,force_restart=True) for camera_id in camera_ids))
    return get_group_storage_volume(group_id,_=None)


def _schedule_document(timezone_name: str, intervals: list[dict[str, object]]) -> dict[str, object]:
    return {"timezone": timezone_name, "intervals": intervals}


def _effective_schedule_view(db: sqlite3.Connection, camera_id: str) -> dict[str, object]:
    policy = effective_policy(db, camera_id, datetime.now(timezone.utc))
    return {
        "effective_recording_mode": policy["mode"],
        "recording_policy_source": policy["source"],
        "schedule_timezone": policy["timezone"],
    }


@router.get("/api/camera-groups/{group_id}/schedule")
def get_group_schedule(
    group_id: str,
    _: CurrentUser,
) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        row = db.execute("SELECT timezone_name,schedule_json,updated_at FROM recording_policy_overrides WHERE scope_type='group' AND scope_id=?", (group_id,)).fetchone()
    if row is None:
        return {"group_id": group_id, "configured": False, "timezone": "UTC", "intervals": []}
    return {"group_id": group_id, "configured": True, "timezone": row["timezone_name"], "intervals": json.loads(row["schedule_json"]), "updated_at": row["updated_at"]}


@router.put("/api/camera-groups/{group_id}/schedule")
async def update_group_schedule(
    group_id: str,
    payload: GroupScheduleRequest,
    request: Request,
    _: Administrator,
) -> dict[str, object]:
    intervals = validate_schedule(payload.timezone, [item.model_dump() for item in payload.intervals])
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        db.execute(
            "INSERT INTO recording_policy_overrides(scope_type,scope_id,timezone_name,schedule_json) VALUES('group',?,?,?) "
            "ON CONFLICT(scope_type,scope_id) DO UPDATE SET timezone_name=excluded.timezone_name,schedule_json=excluded.schedule_json,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (group_id, payload.timezone, json.dumps(intervals, separators=(",", ":"))),
        )
        camera_ids = [row["id"] for row in db.execute("SELECT id FROM cameras WHERE group_id=?", (group_id,)).fetchall()]
    await asyncio.gather(*(request.app.state.recorders.sync_camera(camera_id, force_restart=True) for camera_id in camera_ids))
    return {"group_id": group_id, "configured": True, **_schedule_document(payload.timezone, intervals)}


@router.delete("/api/camera-groups/{group_id}/schedule", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group_schedule(
    group_id: str,
    request: Request,
    _: Administrator,
) -> Response:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        db.execute("DELETE FROM recording_policy_overrides WHERE scope_type='group' AND scope_id=?", (group_id,))
        camera_ids = [row["id"] for row in db.execute("SELECT id FROM cameras WHERE group_id=?", (group_id,)).fetchall()]
    await asyncio.gather(*(request.app.state.recorders.sync_camera(camera_id, force_restart=True) for camera_id in camera_ids))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/cameras/{camera_id}/schedule")
def get_camera_schedule(
    camera_id: str,
    _: CurrentUser,
) -> dict[str, object]:
    with connect() as db:
        camera = db.execute("SELECT id,group_id FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if camera is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        local = db.execute("SELECT timezone_name,schedule_json FROM recording_policy_overrides WHERE scope_type='camera' AND scope_id=?", (camera_id,)).fetchone()
        inherited = None
        if local is None and camera["group_id"]:
            inherited = db.execute("SELECT timezone_name,schedule_json FROM recording_policy_overrides WHERE scope_type='group' AND scope_id=?", (camera["group_id"],)).fetchone()
        source = "camera" if local else "group" if inherited else "camera"
        row = local or inherited
        policy = effective_policy(db, camera_id, datetime.now(timezone.utc))
    return {
        "camera_id": camera_id,
        "inherits_group": local is None and bool(camera["group_id"]),
        "source": source,
        "configured": row is not None,
        "timezone": row["timezone_name"] if row else "UTC",
        "intervals": json.loads(row["schedule_json"]) if row else [],
        "effective_recording_mode": policy["mode"],
    }


@router.put("/api/cameras/{camera_id}/schedule")
async def update_camera_schedule(
    camera_id: str,
    payload: CameraScheduleRequest,
    request: Request,
    _: Administrator,
) -> dict[str, object]:
    if payload.inherit_group:
        intervals = []
    else:
        intervals = validate_schedule(payload.timezone, [item.model_dump() for item in payload.intervals])
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if payload.inherit_group:
            db.execute("DELETE FROM recording_policy_overrides WHERE scope_type='camera' AND scope_id=?", (camera_id,))
        else:
            db.execute(
                "INSERT INTO recording_policy_overrides(scope_type,scope_id,timezone_name,schedule_json) VALUES('camera',?,?,?) "
                "ON CONFLICT(scope_type,scope_id) DO UPDATE SET timezone_name=excluded.timezone_name,schedule_json=excluded.schedule_json,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (camera_id, payload.timezone, json.dumps(intervals, separators=(",", ":"))),
            )
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    return get_camera_schedule(camera_id, _=None)


@router.delete("/api/cameras/{camera_id}/schedule", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera_schedule(
    camera_id: str,
    request: Request,
    _: Administrator,
) -> Response:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        db.execute("DELETE FROM recording_policy_overrides WHERE scope_type='camera' AND scope_id=?", (camera_id,))
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
