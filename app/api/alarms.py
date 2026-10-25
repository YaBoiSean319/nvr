"""The alarm inbox: list, inspect and handle alarms raised by rules."""
from __future__ import annotations

import json
import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from app import alarms
from app.principal import Administrator, CurrentUser, Principal
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


def _visible_alarm(db, alarm_id: str, user: Principal) -> sqlite3.Row:
    """An alarm of a camera this person may see; any other is indistinguishable from one that does not exist."""
    row = db.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone()
    if row is None or not user.access.can_see(row["camera_id"]):
        raise HTTPException(status_code=404, detail="Alarm not found")
    return row


def _detail(alarm_id: str, user: Principal) -> dict[str, object]:
    with connect() as db:
        return alarms.alarm_detail(db, _visible_alarm(db, alarm_id, user))


def _act(alarm_id: str, user: Principal, action) -> dict[str, object]:
    with connect() as db:
        _visible_alarm(db, alarm_id, user)
    try:
        action()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except alarms.AlarmError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _detail(alarm_id, user)


@router.get("/api/alarms")
def list_alarms(
    user: CurrentUser,
    state: Literal["open", "new", "acknowledged", "assigned", "resolved", "all"] = "open",
    camera_id: str | None = Query(default=None, max_length=64),
    rule_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    # Counts follow the camera and rule filters, across every state.
    scope, scope_values = [], []
    for column, value in (("camera_id", camera_id), ("rule_id", rule_id)):
        if value:
            scope.append(f"{column}=?")
            scope_values.append(value)
    if not user.access.unrestricted:
        # Only alarms of cameras this person may see, in the list, the counts and the rule filter alike.
        permitted = sorted(user.access.cameras or ())
        scope.append("camera_id IN (%s)" % ",".join("?" * len(permitted)) if permitted else "0")
        scope_values.extend(permitted)
    clauses, values = list(scope), list(scope_values)
    if state == "open":
        clauses.append("state IN ('new','acknowledged','assigned')")
    elif state != "all":
        clauses.append("state=?")
        values.append(state)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    scoped = " WHERE " + " AND ".join(scope) if scope else ""
    with connect() as db:
        rows = db.execute("SELECT * FROM alarms" + where + " ORDER BY occurred_at DESC,id LIMIT ?", (*values, limit)).fetchall()
        counts = {row["state"]: row["count"] for row in db.execute("SELECT state,count(*) AS count FROM alarms" + scoped + " GROUP BY state", scope_values)}
        # Every rule that has raised an alarm, for the rule filter (operators cannot list rules).
        rules = db.execute("SELECT rule_id AS id,max(rule_name) AS name FROM alarms" + (scoped + " AND" if scoped else " WHERE") + " rule_id IS NOT NULL GROUP BY rule_id ORDER BY name COLLATE NOCASE",
                           scope_values).fetchall()
        return {"alarms": [alarms.alarm_view(db, row) for row in rows],
                "counts": {key: counts.get(key, 0) for key in ("new", "acknowledged", "assigned", "resolved")},
                "rules": [dict(rule) for rule in rules]}


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
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("UPDATE alarm_settings SET resolved_footage_days=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE singleton_id=1",
                       (payload.resolved_footage_days,))
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                       (int(user["id"]), "alarm_settings_updated", "alarm_settings", "1",
                        json.dumps({"resolved_footage_days": payload.resolved_footage_days}, separators=(",", ":"))))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {"resolved_footage_days": payload.resolved_footage_days}


@router.get("/api/alarms/{alarm_id}")
def get_alarm(alarm_id: str, user: CurrentUser) -> dict[str, object]:
    return _detail(alarm_id, user)


@router.post("/api/alarms/{alarm_id}/acknowledge")
def acknowledge_alarm(alarm_id: str, user: CurrentUser) -> dict[str, object]:
    return _act(alarm_id, user, lambda: alarms.acknowledge(alarm_id, int(user["id"])))


@router.post("/api/alarms/{alarm_id}/assign")
def assign_alarm(alarm_id: str, payload: AssignRequest, user: CurrentUser) -> dict[str, object]:
    return _act(alarm_id, user, lambda: alarms.assign(alarm_id, int(user["id"]), payload.user_id))


@router.post("/api/alarms/{alarm_id}/comments", status_code=status.HTTP_201_CREATED)
def comment_on_alarm(alarm_id: str, payload: CommentRequest, user: CurrentUser) -> dict[str, object]:
    return _act(alarm_id, user, lambda: alarms.comment(alarm_id, int(user["id"]), payload.body))


@router.post("/api/alarms/{alarm_id}/resolve")
def resolve_alarm(alarm_id: str, payload: ResolveRequest, user: CurrentUser) -> dict[str, object]:
    note = payload.note.strip() if payload.note else None
    return _act(alarm_id, user, lambda: alarms.resolve(alarm_id, int(user["id"]), note or None))


@router.delete("/api/alarms/{alarm_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alarm(alarm_id: str, user: Administrator) -> Response:
    try:
        alarms.delete(alarm_id, int(user["id"]))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
