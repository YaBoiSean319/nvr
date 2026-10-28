"""External events in (#138): the signed inbound endpoint, event sources and their camera mappings (administrators), Home Assistant, and the events on cameras."""
from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import external_events, home_assistant
from app.external_events import EventRefused, SourceError
from app.principal import Administrator, CurrentUser
from app.store import connect

router = APIRouter()
RATE_PER_MINUTE = 600
_recent: dict[str, deque[float]] = {}


class MappingBody(BaseModel):
    subject: str = Field(min_length=1, max_length=128)
    types: list[str] = Field(default_factory=list, max_length=50)
    camera_ids: list[str] = Field(min_length=1, max_length=200)


class SourceBody(BaseModel):
    """A source. ``secret`` (HTTP) and ``url`` (MQTT broker) are write-only: send them to set or change them, leave them out to keep what is saved."""
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(pattern="^(http|mqtt)$")
    enabled: bool = True
    secret: str | None = Field(default=None, max_length=256)
    url: str | None = Field(default=None, max_length=2048)
    topic: str | None = Field(default=None, max_length=1024)
    allow_private: bool | None = None  # Left out, an MQTT source keeps what it had
    mappings: list[MappingBody] | None = Field(default=None, max_length=500)


class HomeAssistantBody(BaseModel):
    enabled: bool | None = None
    url: str | None = Field(default=None, max_length=2048)
    allow_private: bool | None = None
    discovery_prefix: str | None = Field(default=None, max_length=128)
    node_id: str | None = Field(default=None, max_length=32)
    events_topic: str | None = Field(default=None, max_length=256)


def _audit(db, actor, action: str, resource_id: str, details: dict[str, object]) -> None:
    db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
               (int(actor["id"]), action, "event_source", resource_id, json.dumps(details, separators=(",", ":"))))


def _limited(source_id: str) -> bool:
    """More than RATE_PER_MINUTE events a minute for one source are refused (429), so a runaway sender cannot fill the database."""
    now = time.monotonic()
    window = _recent.setdefault(source_id, deque())
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= RATE_PER_MINUTE:
        return True
    window.append(now)
    if len(_recent) > 1000:
        for key in [key for key, times in _recent.items() if not times or now - times[-1] > 60]:
            del _recent[key]
    return False


# --- the inbound endpoint (signature, not sign-in) ---------------------------------------------------------------------------------------

@router.post("/api/inbound/events/{source_id}", status_code=202)
async def inbound(source_id: str, request: Request) -> JSONResponse:
    """Accept an event from a building system. It must be signed with the source's secret (see docs/external-events.md); anything else is refused."""
    # Counted per sender address as well as per source, so someone who only knows a source's id cannot use up the real sender's allowance.
    if _limited(f"{source_id[:64]}|{request.client.host if request.client else ''}"):
        return JSONResponse({"detail": "Too many events"}, status_code=429)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > external_events.MAX_BODY_BYTES:
        return JSONResponse({"detail": "The event is too large"}, status_code=413)
    # A chunked body declares no length: read it with a cap rather than buffering whatever arrives.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > external_events.MAX_BODY_BYTES:
            return JSONResponse({"detail": "The event is too large"}, status_code=413)
    body = bytes(body)
    try:
        result = external_events.receive_http(source_id, body, request.headers)
    except EventRefused as exc:
        # The reason a sender is not trusted is never told; once it is trusted, why its event was refused is.
        detail = "Not accepted" if exc.status == 401 else exc.reason
        return JSONResponse({"detail": detail}, status_code=exc.status)
    return JSONResponse({"id": result["id"], "cameras": len(result["cameras"]), "alarms": result["alarms"]}, status_code=202)


# --- sources and mappings (administrators) ------------------------------------------------------------------------------------------------

@router.get("/api/event-sources")
def list_sources(_: Administrator) -> dict[str, object]:
    with connect() as db:
        return {"sources": [external_events.source_view(db, row) for row in db.execute("SELECT * FROM event_sources ORDER BY name COLLATE NOCASE,id")]}


def _save(db, body: SourceBody, actor, source_id: str | None) -> str:
    fields = body.model_dump(exclude={"mappings"})
    db.execute("BEGIN IMMEDIATE")
    try:
        saved = external_events.save_source(db, fields, source_id=source_id, actor_id=int(actor["id"]))
        if body.mappings is not None:
            external_events.save_mappings(db, saved, [item.model_dump() for item in body.mappings])
        _audit(db, actor, "event_source_updated" if source_id else "event_source_created", saved, {"name": body.name.strip(), "kind": body.kind})
    except SourceError as exc:
        db.execute("ROLLBACK")
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
    return saved


@router.post("/api/event-sources", status_code=201)
def create_source(body: SourceBody, user: Administrator) -> dict[str, object]:
    with connect() as db:
        saved = _save(db, body, user, None)
        return external_events.source_view(db, db.execute("SELECT * FROM event_sources WHERE id=?", (saved,)).fetchone())


@router.put("/api/event-sources/{source_id}")
def update_source(source_id: str, body: SourceBody, user: Administrator) -> dict[str, object]:
    with connect() as db:
        current = db.execute("SELECT kind FROM event_sources WHERE id=?", (source_id,)).fetchone()
        if current is None or current["kind"] == "home_assistant":
            raise HTTPException(status_code=404, detail="Event source not found")
        if current["kind"] != body.kind:
            raise HTTPException(status_code=422, detail="A source's kind cannot be changed; make a new one")
        _save(db, body, user, source_id)
        return external_events.source_view(db, db.execute("SELECT * FROM event_sources WHERE id=?", (source_id,)).fetchone())


@router.put("/api/event-sources/{source_id}/mappings")
def put_mappings(source_id: str, mappings: list[MappingBody], user: Administrator) -> dict[str, object]:
    """Replace what this source's events are tied to (including Home Assistant's)."""
    with connect() as db:
        if db.execute("SELECT 1 FROM event_sources WHERE id=?", (source_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Event source not found")
        db.execute("BEGIN IMMEDIATE")
        try:
            external_events.save_mappings(db, source_id, [item.model_dump() for item in mappings])
            _audit(db, user, "event_mappings_updated", source_id, {"mappings": len(mappings)})
        except SourceError as exc:
            db.execute("ROLLBACK")
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return {"mappings": external_events.mapping_views(db, source_id)}


@router.delete("/api/event-sources/{source_id}", status_code=204)
def delete_source(source_id: str, user: Administrator) -> None:
    with connect() as db:
        row = db.execute("SELECT name,kind FROM event_sources WHERE id=?", (source_id,)).fetchone()
        if row is None or row["kind"] == "home_assistant":
            raise HTTPException(status_code=404, detail="Event source not found")
        db.execute("DELETE FROM event_sources WHERE id=?", (source_id,))
        _audit(db, user, "event_source_deleted", source_id, {"name": row["name"]})


@router.get("/api/event-sources/{source_id}/events")
def source_events(source_id: str, _: Administrator, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]:
    """What a source has sent, mapped or not, newest first: so a mapping can be written from what really arrives."""
    with connect() as db:
        if db.execute("SELECT 1 FROM event_sources WHERE id=?", (source_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Event source not found")
        return {"events": external_events.recent(db, source_id=source_id, limit=limit)}


# --- Home Assistant (administrators) ------------------------------------------------------------------------------------------------------

@router.get("/api/home-assistant")
def get_home_assistant(_: Administrator) -> dict[str, object]:
    with connect() as db:
        return home_assistant.settings_view(db)


@router.put("/api/home-assistant")
def put_home_assistant(body: HomeAssistantBody, user: Administrator) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            home_assistant.save_settings(db, body.model_dump(exclude_none=True))
            _audit(db, user, "home_assistant_updated", home_assistant.HOME_ASSISTANT_SOURCE, {"enabled": body.enabled})
        except SourceError as exc:
            db.execute("ROLLBACK")
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return home_assistant.settings_view(db)


# --- events on cameras (anyone signed in, within their access) -----------------------------------------------------------------------------

@router.get("/api/external-events")
def list_events(
    user: CurrentUser,
    camera_ids: list[str] | None = Query(default=None, max_length=500),
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    """Events tied to cameras this person may see, newest first; cameras they may not see are left out as if they did not exist."""
    for stamp in (start, end):
        if stamp is not None and stamp.tzinfo is None:
            raise HTTPException(status_code=422, detail="start and end must include a time zone")
    with connect() as db:
        existing = [row[0] for row in db.execute("SELECT id FROM cameras WHERE archived_at IS NULL")]
        wanted = [camera for camera in (camera_ids if camera_ids is not None else existing) if camera in set(existing)]
        return {"events": external_events.recent(db, camera_ids=user.access.only(wanted), start=start, end=end, limit=limit)}
