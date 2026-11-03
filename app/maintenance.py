"""Maintenance windows (#234): while a camera, a group of cameras or the whole site is being worked on, nothing is sent out about it.

What a window quiets: a rule's alarm still happens and is kept (marked with the window) but sends no push, no Live pop-in, no webhook and runs none of the rule's actions;
an incident (camera offline, tamper, ...) raises no webhook. Recording goes on and the state of the camera is still shown. An incident that is still there when the window ends is
announced then, so a camera left broken does not stay silent. Windows are one-off: a start and an end, and an end that cannot be more than a week away.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

MAX_ADMIN_LENGTH = timedelta(days=7)
MAX_OPERATOR_LENGTH = timedelta(hours=8)
MAX_OPERATOR_AHEAD = timedelta(hours=24)  # How far in the future an operator may schedule one to start
MAX_AHEAD = timedelta(days=30)
MIN_LENGTH = timedelta(minutes=1)
MAX_CAMERAS = 200


class WindowError(ValueError):
    """A window cannot be made or changed as asked."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """One text form for every stored time, so they compare as text."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _scope_matches(db: sqlite3.Connection, row: sqlite3.Row, camera_id: str | None) -> bool:
    if row["scope"] == "site":
        return True
    if camera_id is None:
        return False  # A fault that belongs to no camera is quieted only by a window for the whole site
    if row["scope"] == "cameras":
        return camera_id in json.loads(row["camera_ids_json"])
    group = db.execute("SELECT group_id FROM cameras WHERE id=?", (camera_id,)).fetchone()
    return group is not None and group["group_id"] is not None and group["group_id"] in json.loads(row["group_ids_json"])


def covering(db: sqlite3.Connection, camera_id: str | None, at: datetime | None = None) -> sqlite3.Row | None:
    """The window that quiets this camera (or, with no camera, the site) at that moment: the one that ends last, if several do."""
    stamp = iso(at or _now())
    rows = db.execute(
        "SELECT * FROM maintenance_windows WHERE starts_at<=? AND COALESCE(ended_at,ends_at)>? ORDER BY COALESCE(ended_at,ends_at) DESC", (stamp, stamp)).fetchall()
    return next((row for row in rows if _scope_matches(db, row, camera_id)), None)


def count_held(db: sqlite3.Connection, window_id: str, kind: str) -> None:
    column = "suppressed_alarms" if kind == "alarm" else "suppressed_incidents"
    db.execute(f"UPDATE maintenance_windows SET {column}={column}+1 WHERE id=?", (window_id,))


def state_of(row: sqlite3.Row, at: datetime | None = None) -> str:
    stamp = iso(at or _now())
    if row["starts_at"] > stamp:
        return "upcoming"
    return "active" if (row["ended_at"] or row["ends_at"]) > stamp else "ended"


def view(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    camera_ids = json.loads(row["camera_ids_json"])
    group_ids = json.loads(row["group_ids_json"])
    names = {item["id"]: item["name"] for item in db.execute(f"SELECT id,name FROM cameras WHERE id IN ({','.join('?' for _ in camera_ids)})", camera_ids)} if camera_ids else {}
    group_names = {item["id"]: item["name"] for item in db.execute(f"SELECT id,name FROM camera_groups WHERE id IN ({','.join('?' for _ in group_ids)})", group_ids)} if group_ids else {}
    return {
        "id": row["id"], "name": row["name"], "reason": row["reason"], "scope": row["scope"], "state": state_of(row),
        "camera_ids": camera_ids, "camera_names": [names.get(item, "a removed camera") for item in camera_ids],
        "group_ids": group_ids, "group_names": [group_names.get(item, "a removed group") for item in group_ids],
        "starts_at": row["starts_at"], "ends_at": row["ends_at"], "ended_at": row["ended_at"], "created_by": row["created_by_name"],
        "suppressed_alarms": row["suppressed_alarms"], "suppressed_incidents": row["suppressed_incidents"], "created_at": row["created_at"],
    }


def create(
    db: sqlite3.Connection, *, name: str, reason: str, scope: str, camera_ids: list[str], group_ids: list[str], starts_at: datetime, ends_at: datetime,
    actor_id: int, actor_name: str, administrator: bool, can_see: Any,
) -> str:
    """Make a window. Administrators may cover cameras, groups or the site for up to a week; anyone else only cameras they can see, for up to eight hours."""
    now = _now()
    starts_at, ends_at = starts_at.astimezone(timezone.utc), ends_at.astimezone(timezone.utc)
    if ends_at - starts_at < MIN_LENGTH:
        raise WindowError("A maintenance window must last at least a minute")
    if starts_at < now - timedelta(minutes=5):
        raise WindowError("A maintenance window cannot start in the past")
    if starts_at > now + (MAX_AHEAD if administrator else MAX_OPERATOR_AHEAD):
        raise WindowError("That start is too far ahead")
    if ends_at - starts_at > (MAX_ADMIN_LENGTH if administrator else MAX_OPERATOR_LENGTH):
        raise WindowError(f"A maintenance window can last at most {'7 days' if administrator else '8 hours'}: end it and open another if the work goes on")
    if scope not in {"cameras", "groups", "site"}:
        raise WindowError("Choose cameras, groups or the whole site")
    camera_ids, group_ids = list(dict.fromkeys(camera_ids)), list(dict.fromkeys(group_ids))
    if scope != "cameras" and not administrator:
        raise WindowError("Only an administrator can cover groups or the whole site")
    if scope == "cameras":
        if not camera_ids or len(camera_ids) > MAX_CAMERAS:
            raise WindowError("Choose the cameras being worked on")
        group_ids = []
    elif scope == "groups":
        if not group_ids:
            raise WindowError("Choose the groups being worked on")
        camera_ids = []
    else:
        camera_ids, group_ids = [], []
    for camera_id in camera_ids:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None or not can_see(camera_id):
            raise WindowError("Choose cameras you can see")  # The same answer for a camera that does not exist and one that is hidden
    for group_id in group_ids:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise WindowError("Unknown camera group")
    window_id = str(uuid.uuid4())
    stamp = iso(now)
    db.execute(
        "INSERT INTO maintenance_windows(id,name,reason,scope,camera_ids_json,group_ids_json,starts_at,ends_at,created_by,created_by_name,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (window_id, name.strip(), reason.strip(), scope, json.dumps(camera_ids), json.dumps(group_ids), iso(starts_at), iso(ends_at), actor_id, actor_name, stamp, stamp))
    return window_id


def end_now(db: sqlite3.Connection, window_id: str) -> sqlite3.Row:
    """End a window now (a window that has not started is removed: nothing happened in it). Returns the window as it was."""
    row = db.execute("SELECT * FROM maintenance_windows WHERE id=?", (window_id,)).fetchone()
    if row is None:
        raise LookupError("Maintenance window not found")
    state = state_of(row)
    if state == "ended":
        raise WindowError("That maintenance window has already ended")
    if state == "upcoming":
        db.execute("DELETE FROM maintenance_windows WHERE id=?", (window_id,))
    else:
        stamp = iso(_now())
        db.execute("UPDATE maintenance_windows SET ended_at=?,updated_at=? WHERE id=?", (stamp, stamp, window_id))
    return row
