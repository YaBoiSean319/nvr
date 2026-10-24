"""The alarm inbox: list, inspect and handle alarms raised by rules."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from app import alarms
from app.principal import Administrator, CurrentUser
from app.store import connect

router = APIRouter()


class AssignRequest(BaseModel):
    user_id: int


class CommentRequest(BaseModel):
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("body")
    @classmethod
    def trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Write a comment")
        return value


class ResolveRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class AlarmSettingsRequest(BaseModel):
    resolved_footage_days: int = Field(ge=0, le=3650)


def _detail(alarm_id: str) -> dict[str, object]:
    with connect() as db:
        row = db.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Alarm not found")
        return alarms.alarm_detail(db, row)


def _act(alarm_id: str, action) -> dict[str, object]:
    try:
        action()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except alarms.AlarmError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _detail(alarm_id)


@router.get("/api/alarms")
def list_alarms(
    _: CurrentUser,
    state: Literal["open", "new", "acknowledged", "assigned", "resolved", "all"] = "open",
    camera_id: str | None = Query(default=None, max_length=64),
    rule_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    clauses, values = [], []
    if state == "open":
        clauses.append("state IN ('new','acknowledged','assigned')")
    elif state != "all":
        clauses.append("state=?")
        values.append(state)
    for column, value in (("camera_id", camera_id), ("rule_id", rule_id)):
        if value:
            clauses.append(f"{column}=?")
            values.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as db:
        rows = db.execute("SELECT * FROM alarms" + where + " ORDER BY occurred_at DESC,id LIMIT ?", (*values, limit)).fetchall()
        counts = {row["state"]: row["count"] for row in db.execute("SELECT state,count(*) AS count FROM alarms GROUP BY state")}
        return {"alarms": [alarms.alarm_view(db, row) for row in rows],
                "counts": {key: counts.get(key, 0) for key in ("new", "acknowledged", "assigned", "resolved")}}


@router.get("/api/alarms/assignees")
def list_assignees(_: CurrentUser) -> dict[str, object]:
    with connect() as db:
        rows = db.execute("SELECT id,username FROM users ORDER BY username COLLATE NOCASE").fetchall()
    return {"users": [dict(row) for row in rows]}


@router.get("/api/alarm-settings")
def get_alarm_settings(_: CurrentUser) -> dict[str, object]:
    with connect() as db:
        return {"resolved_footage_days": alarms.resolved_footage_days(db)}


@router.put("/api/alarm-settings")
def put_alarm_settings(payload: AlarmSettingsRequest, user: Administrator) -> dict[str, object]:
    with connect() as db:
        db.execute("UPDATE alarm_settings SET resolved_footage_days=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE singleton_id=1",
                   (payload.resolved_footage_days,))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "alarm_settings_updated", "alarm_settings", "1", f'{{"resolved_footage_days":{payload.resolved_footage_days}}}'))
    return {"resolved_footage_days": payload.resolved_footage_days}


@router.get("/api/alarms/{alarm_id}")
def get_alarm(alarm_id: str, _: CurrentUser) -> dict[str, object]:
    return _detail(alarm_id)


@router.post("/api/alarms/{alarm_id}/acknowledge")
def acknowledge_alarm(alarm_id: str, user: CurrentUser) -> dict[str, object]:
    return _act(alarm_id, lambda: alarms.acknowledge(alarm_id, int(user["id"])))


@router.post("/api/alarms/{alarm_id}/assign")
def assign_alarm(alarm_id: str, payload: AssignRequest, user: CurrentUser) -> dict[str, object]:
    return _act(alarm_id, lambda: alarms.assign(alarm_id, int(user["id"]), payload.user_id))


@router.post("/api/alarms/{alarm_id}/comments", status_code=status.HTTP_201_CREATED)
def comment_on_alarm(alarm_id: str, payload: CommentRequest, user: CurrentUser) -> dict[str, object]:
    return _act(alarm_id, lambda: alarms.comment(alarm_id, int(user["id"]), payload.body))


@router.post("/api/alarms/{alarm_id}/resolve")
def resolve_alarm(alarm_id: str, payload: ResolveRequest, user: CurrentUser) -> dict[str, object]:
    note = payload.note.strip() if payload.note else None
    return _act(alarm_id, lambda: alarms.resolve(alarm_id, int(user["id"]), note or None))


@router.delete("/api/alarms/{alarm_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alarm(alarm_id: str, user: Administrator) -> Response:
    try:
        alarms.delete(alarm_id, int(user["id"]))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
