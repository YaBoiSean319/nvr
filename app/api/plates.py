"""Licence plates: the cameras that read them, watch lists (allow and deny), search by full or partial plate, and the plate crops."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from app import plates
from app.audit import record
from app.principal import Administrator, CurrentUser
from app.store import connect

router = APIRouter()

MAX_LISTS = 50
MAX_ENTRIES = 5000
MAX_SEARCH_DAYS = 92
MAX_RESULTS = 200


class CameraPlates(BaseModel):
    enabled: bool
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class WatchListInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["allow", "deny"]
    lookalikes: bool = False

    @field_validator("name")
    @classmethod
    def trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a name for the list")
        return value


class EntryInput(BaseModel):
    pattern: str = Field(min_length=1, max_length=40)
    note: str = Field(default="", max_length=200)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def future_and_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("The expiry must include a time zone")
        if value <= datetime.now(timezone.utc):
            raise ValueError("The expiry must be in the future")
        return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _list_view(db, row, now: str) -> dict[str, Any]:
    entries = db.execute("SELECT id,pattern,note,expires_at,created_at FROM plate_watch_entries WHERE list_id=? ORDER BY pattern,id", (row["id"],)).fetchall()
    return {"id": row["id"], "name": row["name"], "kind": row["kind"], "lookalikes": bool(row["lookalikes"]), "created_at": row["created_at"],
            "entries": [{"id": entry["id"], "pattern": entry["pattern"], "note": entry["note"], "expires_at": entry["expires_at"],
                         "expired": bool(entry["expires_at"] and entry["expires_at"] <= now), "created_at": entry["created_at"]} for entry in entries]}


@router.get("/api/admin/plates")
def get_plates(_: Administrator) -> dict[str, Any]:
    """The plate models' state, each camera's setting, and the watch lists with their entries."""
    now = _now()
    with connect() as db:
        cameras = db.execute("SELECT c.id,c.name,coalesce(p.enabled,0) AS enabled,coalesce(p.min_confidence,0.6) AS min_confidence "
                             "FROM cameras c LEFT JOIN plate_settings p ON p.camera_id=c.id ORDER BY c.name COLLATE NOCASE,c.id").fetchall()
        lists = db.execute("SELECT * FROM plate_watch_lists ORDER BY name COLLATE NOCASE,id").fetchall()
        return {"models": plates.model_status(),
                "cameras": [{"id": row["id"], "name": row["name"], "enabled": bool(row["enabled"]), "min_confidence": row["min_confidence"]} for row in cameras],
                "watch_lists": [_list_view(db, row, now) for row in lists]}


@router.put("/api/admin/plates/cameras/{camera_id}")
def set_camera(camera_id: str, body: CameraPlates, user: Administrator) -> dict[str, Any]:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO plate_settings(camera_id,enabled,min_confidence,updated_at) VALUES(?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET "
                       "enabled=excluded.enabled,min_confidence=excluded.min_confidence,updated_at=excluded.updated_at", (camera_id, int(body.enabled), body.min_confidence, _now()))
            record(db, int(user["id"]), "plates_camera_changed", "camera", camera_id, enabled=body.enabled, min_confidence=body.min_confidence)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {"id": camera_id, "enabled": body.enabled, "min_confidence": body.min_confidence}


def _used_by_rules(db, list_id: str) -> bool:
    return db.execute("SELECT 1 FROM rules WHERE condition_type='plate' AND json_extract(condition_json,'$.watch_list_id')=?", (list_id,)).fetchone() is not None


def _clean_pattern(text: str) -> str:
    pattern = plates.normalise_pattern(text)
    if len([character for character in pattern if character not in "*?"]) < 2:
        raise HTTPException(status_code=422, detail="A plate pattern needs at least two letters or digits (use * for any run of characters and ? for one)")
    return pattern


@router.post("/api/admin/plates/watch-lists", status_code=status.HTTP_201_CREATED)
def create_list(body: WatchListInput, user: Administrator) -> dict[str, Any]:
    list_id = str(uuid.uuid4())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT COUNT(*) FROM plate_watch_lists").fetchone()[0] >= MAX_LISTS:
                raise HTTPException(status_code=422, detail=f"At most {MAX_LISTS} watch lists")
            if db.execute("SELECT 1 FROM plate_watch_lists WHERE name=?", (body.name,)).fetchone():
                raise HTTPException(status_code=409, detail="A watch list with this name already exists")
            db.execute("INSERT INTO plate_watch_lists(id,name,kind,lookalikes,created_by) VALUES(?,?,?,?,?)", (list_id, body.name, body.kind, int(body.lookalikes), int(user["id"])))
            record(db, int(user["id"]), "watch_list_created", "watch_list", list_id, name=body.name, kind=body.kind)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return _list_view(db, db.execute("SELECT * FROM plate_watch_lists WHERE id=?", (list_id,)).fetchone(), _now())


@router.put("/api/admin/plates/watch-lists/{list_id}")
def update_list(list_id: str, body: WatchListInput, user: Administrator) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM plate_watch_lists WHERE id=?", (list_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Watch list not found")
            if row["kind"] != body.kind and _used_by_rules(db, list_id):
                # A rule means something different on an allow list than on a deny list
                raise HTTPException(status_code=409, detail="Rules use this list; change them before changing its kind")
            if db.execute("SELECT 1 FROM plate_watch_lists WHERE name=? AND id<>?", (body.name, list_id)).fetchone():
                raise HTTPException(status_code=409, detail="A watch list with this name already exists")
            db.execute("UPDATE plate_watch_lists SET name=?,kind=?,lookalikes=? WHERE id=?", (body.name, body.kind, int(body.lookalikes), list_id))
            record(db, int(user["id"]), "watch_list_updated", "watch_list", list_id, name=body.name, kind=body.kind, lookalikes=body.lookalikes)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return _list_view(db, db.execute("SELECT * FROM plate_watch_lists WHERE id=?", (list_id,)).fetchone(), _now())


@router.delete("/api/admin/plates/watch-lists/{list_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_list(list_id: str, user: Administrator) -> Response:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT name FROM plate_watch_lists WHERE id=?", (list_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Watch list not found")
            if _used_by_rules(db, list_id):
                raise HTTPException(status_code=409, detail="Rules use this list; remove them first")
            db.execute("DELETE FROM plate_watch_lists WHERE id=?", (list_id,))
            record(db, int(user["id"]), "watch_list_deleted", "watch_list", list_id, name=row["name"])
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/admin/plates/watch-lists/{list_id}/entries", status_code=status.HTTP_201_CREATED)
def add_entry(list_id: str, body: EntryInput, user: Administrator) -> dict[str, Any]:
    pattern = _clean_pattern(body.pattern)
    entry_id = str(uuid.uuid4())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT 1 FROM plate_watch_lists WHERE id=?", (list_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="Watch list not found")
            if db.execute("SELECT COUNT(*) FROM plate_watch_entries WHERE list_id=?", (list_id,)).fetchone()[0] >= MAX_ENTRIES:
                raise HTTPException(status_code=422, detail=f"At most {MAX_ENTRIES} entries on a list")
            if db.execute("SELECT 1 FROM plate_watch_entries WHERE list_id=? AND pattern=?", (list_id, pattern)).fetchone():
                raise HTTPException(status_code=409, detail="This plate is already on the list")
            db.execute("INSERT INTO plate_watch_entries(id,list_id,pattern,note,expires_at,created_by) VALUES(?,?,?,?,?,?)",
                       (entry_id, list_id, pattern, body.note.strip(), body.expires_at.astimezone(timezone.utc).isoformat() if body.expires_at else None, int(user["id"])))
            record(db, int(user["id"]), "watch_list_entry_added", "watch_list", list_id, pattern=pattern)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return _list_view(db, db.execute("SELECT * FROM plate_watch_lists WHERE id=?", (list_id,)).fetchone(), _now())


@router.delete("/api/admin/plates/watch-lists/{list_id}/entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_entry(list_id: str, entry_id: str, user: Administrator) -> Response:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT pattern FROM plate_watch_entries WHERE id=? AND list_id=?", (entry_id, list_id)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Entry not found")
            db.execute("DELETE FROM plate_watch_entries WHERE id=?", (entry_id,))
            record(db, int(user["id"]), "watch_list_entry_removed", "watch_list", list_id, pattern=row["pattern"])
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/plates/search")
def search_plates(
    user: CurrentUser,
    q: str = Query(min_length=1, max_length=40),
    start: str | None = Query(default=None, max_length=64),
    end: str | None = Query(default=None, max_length=64),
    camera_ids: list[str] | None = Query(default=None, max_length=500),
    lookalikes: bool = True,
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=60, ge=1, le=MAX_RESULTS),
) -> dict[str, Any]:
    """Plate reads matching a full or partial plate (``*`` any run of characters, ``?`` one character) on the cameras the person may see, newest first.

    Text without a wildcard finds plates containing it; with one the pattern must match the whole plate. ``lookalikes`` (on by default) also finds
    plates differing only in characters cameras confuse (O, Q or D and 0, I or L and 1, S and 5, B and 8, Z and 2). The range defaults to the last 7 days."""
    glob = plates.search_glob(q)
    if glob is None:
        raise HTTPException(status_code=422, detail="Enter letters, digits or wildcards to search for")
    now = datetime.now(timezone.utc)
    try:
        end_time = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else now
        start_time = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else end_time - timedelta(days=7)
        if start_time.tzinfo is None or end_time.tzinfo is None:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="start and end must be timestamps with a time zone") from None
    if (end_time - start_time).total_seconds() <= 0 or (end_time - start_time).days > MAX_SEARCH_DAYS:
        raise HTTPException(status_code=422, detail=f"The range must be positive and at most {MAX_SEARCH_DAYS} days")
    with connect() as db:
        names = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM cameras")}
        # Cameras this person may not see are not searched, and are not distinguishable from cameras that do not exist.
        wanted = [camera_id for camera_id in (camera_ids or names) if camera_id in names]
        visible = user.access.only(wanted)
        rows = plates.search(db, q, camera_ids=visible, start=start_time.astimezone(timezone.utc).isoformat(), end=end_time.astimezone(timezone.utc).isoformat(),
                             lookalikes=lookalikes, min_confidence=min_confidence, limit=limit + 1, order=order)
        reading = {row["camera_id"] for row in db.execute("SELECT camera_id FROM plate_settings WHERE enabled=1")}
    return {"query": q, "pattern": glob, "lookalikes": lookalikes, "start": start_time.astimezone(timezone.utc).isoformat(), "end": end_time.astimezone(timezone.utc).isoformat(),
            "results": [plates.read_view(row) for row in rows[:limit]], "truncated": len(rows) > limit,
            "cameras_reading": sorted(camera_id for camera_id in visible if camera_id in reading),
            "note": "Reads are what the plate model made of the picture, with a confidence: check them against the footage before acting on one."}


@router.get("/api/plates/reads/{read_id}/crop")
def read_crop(read_id: str, user: CurrentUser) -> Response:
    """The picture a plate was read from."""
    with connect() as db:
        row = db.execute("SELECT camera_id,crop FROM plate_reads WHERE id=?", (read_id,)).fetchone()
    if row is None or row["crop"] is None or not user.access.can_see(row["camera_id"]):
        raise HTTPException(status_code=404, detail="Plate picture not found")
    return Response(bytes(row["crop"]), media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
