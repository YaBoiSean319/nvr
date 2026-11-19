"""The alarm inbox: list, inspect and handle alarms raised by rules."""
from __future__ import annotations

import json
import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, model_validator, field_validator

from app import console
from app import alarms
from app.access import may_view_camera
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
    resolved_footage_days: int | None = Field(default=None, ge=0, le=3650)
    # How long resolved alarms and rule events are kept (#349); 90 days unless an administrator changes it.
    history_days: int | None = Field(default=None, ge=7, le=3650)

    @model_validator(mode="after")
    def one_given(self) -> "AlarmSettingsRequest":
        if self.resolved_footage_days is None and self.history_days is None:
            raise ValueError("Give resolved_footage_days, history_days or both")
        return self


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
@console.federated(console.split_alarms, console.combine_alarms)
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
    permission, permission_values = [], []
    if not user.access.unrestricted:
        # Only alarms of cameras this person may see, in the list, the counts and the rule filter alike.
        permitted = sorted(user.access.cameras or ())
        permission.append("camera_id IN (%s)" % ",".join("?" * len(permitted)) if permitted else "0")
        permission_values.extend(permitted)
    scope.extend(permission)
    scope_values.extend(permission_values)
    clauses, values = list(scope), list(scope_values)
    if state == "open":
        clauses.append("state IN ('new','acknowledged','assigned')")
    elif state != "all":
        clauses.append("state=?")
        values.append(state)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    scoped = " WHERE " + " AND ".join(scope) if scope else ""
    with connect() as db:
        # The default view (every open alarm, newest first) is read from the index made for it. Without table statistics SQLite would
        # otherwise take the state index and sort every open alarm on each poll (#349); with a camera or rule filter its own index is better.
        source = "alarms INDEXED BY alarms_open_order_idx" if state == "open" and not camera_id and not rule_id and user.access.unrestricted else "alarms"
        rows = db.execute(f"SELECT * FROM {source}" + where + " ORDER BY occurred_at DESC,id LIMIT ?", (*values, limit)).fetchall()
        counts = {row["state"]: row["count"] for row in db.execute("SELECT state,count(*) AS count FROM alarms" + scoped + " GROUP BY state", scope_values)}
        # Every rule that has raised an alarm, for the rule filter (operators cannot list rules).
        rules = db.execute("SELECT rule_id AS id,max(rule_name) AS name FROM alarms WHERE " + "".join(f"{clause} AND " for clause in permission) + "rule_id IS NOT NULL GROUP BY rule_id ORDER BY name COLLATE NOCASE",
                           permission_values).fetchall()
        return {"alarms": alarms.alarm_views(db, rows),
                "counts": {key: counts.get(key, 0) for key in ("new", "acknowledged", "assigned", "resolved")},
                "rules": [dict(rule) for rule in rules]}


@router.get("/api/alarms/assignees")
def list_assignees(_: CurrentUser, camera_id: str | None = Query(default=None, max_length=64)) -> dict[str, object]:
    """Who an alarm can be assigned to: with a camera, the people who can see it."""
    with connect() as db:
        rows = db.execute("SELECT id,username FROM users ORDER BY username COLLATE NOCASE").fetchall()
        if camera_id:
            rows = [row for row in rows if may_view_camera(db, int(row["id"]), camera_id)]
    return {"users": [dict(row) for row in rows]}


@router.get("/api/alarm-settings")
def get_alarm_settings(_: CurrentUser) -> dict[str, object]:
    with connect() as db:
        return alarms.settings(db)


@router.put("/api/alarm-settings")
def put_alarm_settings(payload: AlarmSettingsRequest, user: Administrator) -> dict[str, object]:
    changes = payload.model_dump(exclude_none=True)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            before = alarms.settings(db)
            db.execute(f"UPDATE alarm_settings SET {','.join(f'{key}=?' for key in changes)},updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE singleton_id=1",
                       tuple(changes.values()))
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                       (int(user["id"]), "alarm_settings_updated", "alarm_settings", "1",
                        json.dumps({**changes, "before": {key: before[key] for key in changes}}, separators=(",", ":"))))
            saved = alarms.settings(db)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return saved


@router.get("/api/alarms/{alarm_id}")
def get_alarm(alarm_id: str, user: CurrentUser) -> dict[str, object]:
    return _detail(alarm_id, user)


@router.post("/api/alarms/{alarm_id}/acknowledge")
def acknowledge_alarm(alarm_id: str, user: CurrentUser) -> dict[str, object]:
    return _act(alarm_id, user, lambda: alarms.acknowledge(alarm_id, int(user["id"])))


@router.post("/api/alarms/{alarm_id}/assign")
def assign_alarm(alarm_id: str, payload: AssignRequest, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        alarm = _visible_alarm(db, alarm_id, user)
        # Someone who cannot see the camera cannot be handed its alarm.
        if db.execute("SELECT 1 FROM users WHERE id=?", (payload.user_id,)).fetchone() and not may_view_camera(db, payload.user_id, alarm["camera_id"]):
            raise HTTPException(status_code=422, detail="That person cannot see this camera")
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
