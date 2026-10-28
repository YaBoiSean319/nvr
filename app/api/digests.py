"""Event digests: read them, make one on demand, and (administrators) schedule them per shift and set where they are delivered (#136)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app import access, digests
from app.api.rules import ActionDefinition
from app.principal import Administrator, CurrentUser
from app.store import connect

router = APIRouter()


class Scope(BaseModel):
    site: bool = False
    group_ids: list[str] = Field(default_factory=list, max_length=200)
    camera_ids: list[str] = Field(default_factory=list, max_length=500)


class Shift(BaseModel):
    days: list[int] = Field(min_length=1, max_length=7)
    start: str = Field(max_length=5)
    end: str = Field(max_length=5)


class AfterHours(BaseModel):
    start: str = Field(default="22:00", max_length=5)
    end: str = Field(default="06:00", max_length=5)


class ScheduleBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    scope: Scope
    recipient_user_id: int
    shift: Shift
    after_hours: AfterHours = Field(default_factory=AfterHours)
    summarise: bool = False
    actions: list[ActionDefinition] = Field(default_factory=list, max_length=10)


class ModelBody(BaseModel):
    enabled: bool = False
    url: str | None = Field(default=None, max_length=2048)
    model: str = Field(default="", max_length=200)
    allow_private: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _range(start: str, end: str) -> tuple[datetime, datetime]:
    try:
        start_time, end_time = datetime.fromisoformat(start.replace("Z", "+00:00")), datetime.fromisoformat(end.replace("Z", "+00:00"))
        if start_time.tzinfo is None or end_time.tzinfo is None:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="start and end must be timestamps with a time zone") from None
    if (end_time - start_time).total_seconds() <= 0 or (end_time - start_time) > digests.MAX_RANGE:
        raise HTTPException(status_code=422, detail=f"The range must be positive and at most {digests.MAX_RANGE.days} days")
    return start_time.astimezone(timezone.utc), end_time.astimezone(timezone.utc)


def _viewer(user) -> tuple[set[str] | None, bool]:
    return (None, True) if user.access.unrestricted else (set(user.access.cameras or ()), False)


def _view(row, body: dict[str, Any], user, *, with_text: bool) -> dict[str, Any]:
    visible, unrestricted = _viewer(user)
    restricted = digests.restrict(body, visible, unrestricted)
    # The summary was written from everything the recipient could see when it was made. For a reader whose view is now narrower than that, it may
    # mention what they cannot see, so it is left out.
    summary = None if digests.narrowed(body, restricted) else row["summary"]
    result = {"id": row["id"], "name": row["name"], "schedule_id": row["schedule_id"], "period_start": row["period_start"], "period_end": row["period_end"], "created_at": row["created_at"],
              "headline": digests.headline(restricted), "summary": summary, "summary_note": digests.SUMMARY_NOTE if summary else None,
              "summary_error": row["summary_error"], "digest": restricted}
    if with_text:
        result["text"] = digests.render_text(row["name"], restricted, summary)
    return result


# --- reading and on-demand digests (anyone signed in, within their access) -------------------------------------------------------------------

@router.get("/api/digests")
def list_digests(user: CurrentUser, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, object]:
    """The digests made for this person (an administrator sees every schedule's), newest first."""
    with connect() as db:
        rows = db.execute("SELECT * FROM digests ORDER BY period_end DESC,id LIMIT ?", (limit * 4 if user.is_administrator else limit,)).fetchall() if user.is_administrator else \
            db.execute("SELECT * FROM digests WHERE recipient_user_id=? ORDER BY period_end DESC,id LIMIT ?", (int(user["id"]), limit)).fetchall()
        items = [{key: view[key] for key in ("id", "name", "schedule_id", "period_start", "period_end", "created_at", "headline")}
                 for view in (_view(row, json.loads(row["body_json"]), user, with_text=False) for row in rows[:limit])]
    return {"digests": items}


@router.get("/api/digests/preview")
def preview(
    user: CurrentUser,
    start: str = Query(max_length=64),
    end: str = Query(max_length=64),
    site: bool = False,
    group_ids: list[str] | None = Query(default=None, max_length=200),
    camera_ids: list[str] | None = Query(default=None, max_length=500),
    after_hours_start: str = Query(default="22:00", max_length=5),
    after_hours_end: str = Query(default="06:00", max_length=5),
) -> dict[str, object]:
    """A digest of the range for the site, groups or cameras, made now from stored data and not kept. It covers only what the reader may see."""
    start_time, end_time = _range(start, end)
    window = {"start": after_hours_start, "end": after_hours_end}
    try:
        digests._clock(after_hours_start), digests._clock(after_hours_end)
        with connect() as db:
            groups, picked = list(group_ids or []), list(camera_ids or [])
            if not user.access.unrestricted:
                # What this person may not see is dropped before anything is checked, so an unknown id and a hidden one are told apart by nothing.
                groups = [group for group in groups if group in (user.access.groups or ())]
                picked = user.access.only(picked)
            whole_site = site or not (group_ids or camera_ids)
            scope = digests.validate_scope(db, {"site": whole_site, "group_ids": groups, "camera_ids": picked}) if (whole_site or groups or picked) else {"site": False, "group_ids": [], "camera_ids": []}
            wanted = digests.scope_cameras(db, scope) if (scope["site"] or scope["group_ids"] or scope["camera_ids"]) else []
            cameras = user.access.only(wanted)  # A camera that may not be seen is left out, as if it did not exist
            body = digests.build(db, cameras, start_time, end_time, unrestricted=user.access.unrestricted, include_system=user.access.unrestricted and bool(scope["site"]),
                                 public_url="", after_hours=window)
    except digests.DigestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    name = "Digest"
    return {"id": None, "name": name, "period_start": body["period"]["start"], "period_end": body["period"]["end"], "headline": digests.headline(body), "summary": None,
            "summary_note": None, "summary_error": None, "digest": body, "text": digests.render_text(name, body)}


# --- schedules and settings (administrators) -------------------------------------------------------------------------------------------------

@router.get("/api/digests/schedules")
def list_schedules(_: Administrator) -> dict[str, object]:
    with connect() as db:
        return {"schedules": [digests.schedule_view(db, row) for row in db.execute("SELECT * FROM digest_schedules ORDER BY name COLLATE NOCASE,id")]}


def _validated(db, body: ScheduleBody) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        scope = digests.validate_scope(db, body.scope.model_dump())
        shift = digests.validate_shift(body.shift.model_dump())
        window = body.after_hours.model_dump()
        digests._clock(window["start"]), digests._clock(window["end"])
    except digests.DigestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if db.execute("SELECT 1 FROM users WHERE id=?", (body.recipient_user_id,)).fetchone() is None:
        raise HTTPException(status_code=422, detail="Choose who the digest is for")
    if body.summarise and not digests.model_settings(db)["summary_enabled"]:
        raise HTTPException(status_code=422, detail="Configure and turn on the summarising model first")
    return scope, shift, window


def _audit(db, actor, action: str, schedule_id: str, details: dict[str, object]) -> None:
    db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
               (int(actor["id"]), action, "digest_schedule", schedule_id, json.dumps(details, separators=(",", ":"))))


@router.post("/api/digests/schedules", status_code=201)
def create_schedule(body: ScheduleBody, user: Administrator) -> dict[str, object]:
    schedule_id = str(uuid.uuid4())
    with connect() as db:
        if db.execute("SELECT COUNT(*) FROM digest_schedules").fetchone()[0] >= digests.MAX_SCHEDULES:
            raise HTTPException(status_code=422, detail=f"At most {digests.MAX_SCHEDULES} digest schedules")
        scope, shift, window = _validated(db, body)
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO digest_schedules(id,name,enabled,scope_json,recipient_user_id,shift_json,after_hours_json,summarise,last_period_end,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (schedule_id, body.name.strip(), int(body.enabled), json.dumps(scope), body.recipient_user_id, json.dumps(shift), json.dumps(window), int(body.summarise),
                        digests.last_finished_shift_end(db, shift), int(user["id"])))
            digests.save_actions(db, schedule_id, [action.model_dump() for action in body.actions])
            _audit(db, user, "digest_schedule_created", schedule_id, {"name": body.name.strip(), "actions": len(body.actions)})
        except digests.DigestError as exc:
            db.execute("ROLLBACK")
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return digests.schedule_view(db, db.execute("SELECT * FROM digest_schedules WHERE id=?", (schedule_id,)).fetchone())


@router.put("/api/digests/schedules/{schedule_id}")
def update_schedule(schedule_id: str, body: ScheduleBody, user: Administrator) -> dict[str, object]:
    with connect() as db:
        current = db.execute("SELECT * FROM digest_schedules WHERE id=?", (schedule_id,)).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="Digest schedule not found")
        scope, shift, window = _validated(db, body)
        db.execute("BEGIN IMMEDIATE")
        try:
            # A changed shift starts again from the shift that last finished, so editing never makes a flood of old digests.
            last = digests.last_finished_shift_end(db, shift) if json.loads(current["shift_json"]) != shift else current["last_period_end"]
            db.execute("UPDATE digest_schedules SET name=?,enabled=?,scope_json=?,recipient_user_id=?,shift_json=?,after_hours_json=?,summarise=?,last_period_end=?,updated_at=? WHERE id=?",
                       (body.name.strip(), int(body.enabled), json.dumps(scope), body.recipient_user_id, json.dumps(shift), json.dumps(window), int(body.summarise), last,
                        _now().isoformat(), schedule_id))
            digests.save_actions(db, schedule_id, [action.model_dump() for action in body.actions])
            _audit(db, user, "digest_schedule_updated", schedule_id, {"name": body.name.strip(), "actions": len(body.actions)})
        except digests.DigestError as exc:
            db.execute("ROLLBACK")
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return digests.schedule_view(db, db.execute("SELECT * FROM digest_schedules WHERE id=?", (schedule_id,)).fetchone())


@router.delete("/api/digests/schedules/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: str, user: Administrator) -> None:
    with connect() as db:
        row = db.execute("SELECT name FROM digest_schedules WHERE id=?", (schedule_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Digest schedule not found")
        db.execute("DELETE FROM digest_schedules WHERE id=?", (schedule_id,))
        _audit(db, user, "digest_schedule_deleted", schedule_id, {"name": row["name"]})


@router.post("/api/digests/schedules/{schedule_id}/run")
def run_schedule(schedule_id: str, user: Administrator) -> dict[str, object]:
    """Make the digest of the shift that last finished now, and deliver it. If that shift already has one, it is returned and nothing is sent again."""
    with connect() as db:
        schedule = db.execute("SELECT * FROM digest_schedules WHERE id=?", (schedule_id,)).fetchone()
        if schedule is None:
            raise HTTPException(status_code=404, detail="Digest schedule not found")
        start, end = digests.latest_shift(json.loads(schedule["shift_json"]), digests.site_zone(db), _now())
        already = db.execute("SELECT 1 FROM digests WHERE schedule_id=? AND period_end=?", (schedule_id, end.isoformat())).fetchone() is not None
        composed = None if already else digests.compose(db, schedule, start, end)  # Outside the write lock: this may wait for the model
        db.execute("BEGIN IMMEDIATE")
        try:
            digest_id = None if composed is None else digests.store(db, schedule, start, end, composed)
            existing = digest_id is None
            if existing:
                digest_id = db.execute("SELECT id FROM digests WHERE schedule_id=? AND period_end=?", (schedule_id, end.isoformat())).fetchone()["id"]
            db.execute("UPDATE digest_schedules SET last_period_end=max(last_period_end,?) WHERE id=?", (end.isoformat(), schedule_id))
            _audit(db, user, "digest_run", schedule_id, {"digest_id": digest_id, "already_made": existing})
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {"digest_id": digest_id, "already_made": existing}


@router.get("/api/digests/schedules/{schedule_id}/deliveries")
def schedule_deliveries(schedule_id: str, _: Administrator) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM digest_schedules WHERE id=?", (schedule_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Digest schedule not found")
    return {"deliveries": digests.deliveries(schedule_id)}


@router.get("/api/digests/settings")
def get_settings(_: Administrator) -> dict[str, object]:
    with connect() as db:
        return digests.model_settings(db)


@router.put("/api/digests/settings")
def put_settings(body: ModelBody, user: Administrator) -> dict[str, object]:
    with connect() as db:
        try:
            digests.save_model_settings(db, enabled=body.enabled, url=body.url, model=body.model, allow_private=body.allow_private)
        except digests.DigestError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "digest_model_updated", "digest_settings", "1", json.dumps({"enabled": body.enabled, "model": body.model.strip()})))
        return digests.model_settings(db)


@router.get("/api/digests/{digest_id}")
def get_digest(digest_id: str, user: CurrentUser) -> dict[str, object]:
    """A digest, as this reader may see it. One made for someone else is not theirs to read (administrators excepted) and answers like a missing one."""
    with connect() as db:
        row = db.execute("SELECT * FROM digests WHERE id=?", (digest_id,)).fetchone()
        if row is None or not (user.is_administrator or row["recipient_user_id"] == int(user["id"])):
            raise HTTPException(status_code=404, detail="Digest not found")
        return _view(row, json.loads(row["body_json"]), user, with_text=True)
