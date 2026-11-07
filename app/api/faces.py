"""The face watch list and its switches (#253; see app/faces.py and docs/face-watch.md). Administrators only; every change is audited."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, model_validator

from app import face_models, faces
from app.principal import Administrator
from app.store import connect

router = APIRouter()


def _raise(error: faces.FaceError) -> None:
    raise HTTPException(status_code=error.status, detail=error.message) from error


class Box(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)


class Point(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class FindRequest(BaseModel):
    camera_id: str = Field(min_length=1, max_length=64)
    at: datetime

    @model_validator(mode="after")
    def has_zone(self) -> "FindRequest":
        if self.at.tzinfo is None:
            raise ValueError("The time must include a timezone")
        return self


class AddRequest(FindRequest):
    box: Box | None = None
    point: Point | None = None
    entry_id: str | None = Field(default=None, max_length=64)  # Add this face to an existing entry (a second picture makes a return likelier to be noticed)
    name: str | None = Field(default=None, max_length=120)
    reason: str | None = Field(default=None, max_length=500)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def complete(self) -> "AddRequest":
        if (self.box is None) == (self.point is None):
            raise ValueError("Choose the face by its box or by a point on it")
        if self.entry_id is None:
            if not (self.name or "").strip():
                raise ValueError("Enter a name or label for the entry")
            if not (self.reason or "").strip():
                raise ValueError("Enter why this person is on the list")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("The expiry must include a timezone")
        return self


class UpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    reason: str | None = Field(default=None, max_length=500)
    expires_at: datetime | None = None
    clear_expiry: bool = False
    enabled: bool | None = None

    @model_validator(mode="after")
    def valid(self) -> "UpdateRequest":
        if self.name is not None and not self.name.strip():
            raise ValueError("The name cannot be empty")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("The reason cannot be empty")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("The expiry must include a timezone")
        return self


class CameraRequest(BaseModel):
    enabled: bool


class SettingsRequest(BaseModel):
    match_threshold: float = Field(ge=0.25, le=0.9)


def _expiry(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


@router.get("/api/faces/status")
def face_status(_: Administrator) -> dict[str, Any]:
    with connect() as db:
        return {"models": face_models.status(), "match_threshold": faces.match_threshold(db), "default_threshold": face_models.DEFAULT_MATCH_THRESHOLD,
                "cameras_enabled": len(faces.cameras_enabled(db)), "entries": len(faces.list_entries(db))}


@router.put("/api/faces/settings")
def put_settings(payload: SettingsRequest, user: Administrator) -> dict[str, Any]:
    with connect() as db:
        before = faces.match_threshold(db)
        db.execute("UPDATE face_settings SET match_threshold=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE singleton_id=1", (payload.match_threshold,))
        faces.audit(db, int(user["id"]), "face_settings_updated", "1", {"from": before, "to": payload.match_threshold})
    return {"match_threshold": payload.match_threshold}


@router.get("/api/faces/watch")
def list_watch(_: Administrator) -> dict[str, Any]:
    with connect() as db:
        return {"entries": faces.list_entries(db), "match_threshold": faces.match_threshold(db)}


@router.post("/api/faces/find")
async def find(payload: FindRequest, user: Administrator) -> dict[str, Any]:
    """The faces in a camera's recorded picture at a moment, so one can be chosen for the list."""
    try:
        frame, found = await faces.find_faces(payload.camera_id, payload.at)
    except faces.FaceError as error:
        _raise(error)
    jpeg = await asyncio.to_thread(faces.frame_jpeg, frame)
    with connect() as db:
        faces.audit(db, int(user["id"]), "face_search_viewed", payload.camera_id, {"at": payload.at.astimezone(timezone.utc).isoformat(), "faces": len(found)})
    return {"camera_id": payload.camera_id, "at": payload.at.astimezone(timezone.utc).isoformat(), "picture": base64.b64encode(jpeg).decode() if jpeg else None, "faces": [{key: value for key, value in item.items() if key != "_face"} for item in found]}


@router.post("/api/faces/watch", status_code=201)
async def add(payload: AddRequest, user: Administrator) -> dict[str, Any]:
    """Put the chosen face on the list (a new entry), or add it to an existing entry as another picture of that person."""
    try:
        frame, found = await faces.find_faces(payload.camera_id, payload.at)
        item = faces.choose(found, payload.box.model_dump() if payload.box else None, payload.point.model_dump() if payload.point else None)
    except faces.FaceError as error:
        _raise(error)
    if not item["usable"]:
        raise HTTPException(status_code=422, detail=item["reason"])
    face = item["_face"]
    # The picture is made (an FFmpeg run) before the write lock is taken, and off the event loop.
    picture = None if payload.entry_id else await asyncio.to_thread(faces.picture_of, frame, face)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if payload.entry_id:
                faces.get_entry(db, payload.entry_id)
                entry_id, action = payload.entry_id, "face_watch_picture_added"
            else:
                entry_id = faces.create_entry(db, payload.name.strip(), payload.reason.strip(), _expiry(payload.expires_at), picture, int(user["id"]))
                action = "face_watch_added"
            faces.add_embedding(db, entry_id, face, payload.camera_id, payload.at)
            faces.audit(db, int(user["id"]), action, entry_id, {"camera_id": payload.camera_id, "at": payload.at.astimezone(timezone.utc).isoformat(),
                                                                "name": payload.name.strip() if payload.name else None, "reason": payload.reason.strip() if payload.reason else None,
                                                                "expires_at": _expiry(payload.expires_at)})
        except faces.FaceError as error:
            db.execute("ROLLBACK")
            _raise(error)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return faces.get_entry(db, entry_id)


@router.patch("/api/faces/watch/{entry_id}")
def update(entry_id: str, payload: UpdateRequest, user: Administrator) -> dict[str, Any]:
    with connect() as db:
        try:
            before = faces.get_entry(db, entry_id)
        except faces.FaceError as error:
            _raise(error)
        fields: dict[str, Any] = {}
        if payload.name is not None:
            fields["name"] = payload.name.strip()
        if payload.reason is not None:
            fields["reason"] = payload.reason.strip()
        if payload.clear_expiry:
            fields["expires_at"] = None
        elif payload.expires_at is not None:
            fields["expires_at"] = _expiry(payload.expires_at)
        if payload.enabled is not None:
            fields["enabled"] = int(payload.enabled)
        if fields:
            assignments = ",".join(f"{name}=?" for name in fields)
            db.execute(f"UPDATE face_watch_entries SET {assignments},updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (*fields.values(), entry_id))
            faces.audit(db, int(user["id"]), "face_watch_updated", entry_id, {"changed": sorted(fields), "was": {name: before.get(name) for name in fields if name in before}})
        return faces.get_entry(db, entry_id)


@router.delete("/api/faces/watch/{entry_id}", status_code=204)
def remove(entry_id: str, user: Administrator) -> Response:
    with connect() as db:
        try:
            entry = faces.get_entry(db, entry_id)
        except faces.FaceError as error:
            _raise(error)
        db.execute("DELETE FROM face_watch_entries WHERE id=?", (entry_id,))  # Its embeddings go with it (ON DELETE CASCADE)
        faces.audit(db, int(user["id"]), "face_watch_removed", entry_id, {"name": entry["name"], "reason": entry["reason"]})
    return Response(status_code=204)


@router.get("/api/faces/watch/{entry_id}/picture")
def picture(entry_id: str, _: Administrator) -> Response:
    with connect() as db:
        try:
            data = faces.entry_picture(db, entry_id)
        except faces.FaceError as error:
            _raise(error)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/api/faces/cameras")
def cameras(_: Administrator) -> dict[str, Any]:
    with connect() as db:
        enabled = set(faces.cameras_enabled(db))
        rows = db.execute("SELECT id,name FROM cameras WHERE archived_at IS NULL ORDER BY name,id").fetchall()
    return {"cameras": [{"camera_id": row["id"], "name": row["name"], "enabled": row["id"] in enabled} for row in rows], "models_available": face_models.installed()}


@router.put("/api/faces/cameras/{camera_id}")
def set_camera(camera_id: str, payload: CameraRequest, user: Administrator) -> dict[str, Any]:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=? AND archived_at IS NULL", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        db.execute("INSERT INTO face_camera_settings(camera_id,enabled) VALUES(?,?) ON CONFLICT(camera_id) DO UPDATE SET enabled=excluded.enabled,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                   (camera_id, int(payload.enabled)))
        faces.audit(db, int(user["id"]), "face_camera_switched", camera_id, {"enabled": payload.enabled})
    return {"camera_id": camera_id, "enabled": payload.enabled}


@router.get("/api/faces/sightings")
def sightings(user: Administrator, limit: int = 50) -> dict[str, Any]:
    """The most recent matches, newest first; looking at them is itself audited."""
    limit = max(1, min(limit, 200))
    with connect() as db:
        rows = db.execute(
            "SELECT s.id,s.entry_id,e.name AS entry_name,s.camera_id,c.name AS camera_name,s.track_id,s.seen_at,s.similarity,(s.picture IS NOT NULL) AS has_picture "
            "FROM face_sightings s JOIN face_watch_entries e ON e.id=s.entry_id JOIN cameras c ON c.id=s.camera_id ORDER BY s.seen_at DESC,s.id DESC LIMIT ?", (limit,)).fetchall()
        faces.audit(db, int(user["id"]), "face_sightings_viewed", "all", {"shown": len(rows)})
    return {"sightings": [{**dict(row), "has_picture": bool(row["has_picture"])} for row in rows]}


@router.get("/api/faces/sightings/{sighting_id}/picture")
def sighting_picture(sighting_id: str, user: Administrator) -> Response:
    with connect() as db:
        row = db.execute("SELECT picture,entry_id FROM face_sightings WHERE id=?", (sighting_id,)).fetchone()
        if row is None or row["picture"] is None:
            raise HTTPException(status_code=404, detail="No picture for that sighting")
        faces.audit(db, int(user["id"]), "face_sighting_picture_viewed", row["entry_id"], {"sighting_id": sighting_id})
    return Response(bytes(row["picture"]), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
