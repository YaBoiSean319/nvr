"""Alarms: what a rule saw, and how people handled it.

Every rule event raises one alarm. An alarm moves from ``new`` to ``acknowledged``
or ``assigned`` (to a user) and finally ``resolved``; comments can be added at any
time. Each step is kept in the alarm's history, written to the audit log and, for
state changes, sent as a signed ``alarm.*`` webhook.

The footage around an alarm (``CLIP_BEFORE`` to ``CLIP_AFTER``) is kept by
retention until the alarm is resolved and the configured number of days has passed.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

CLIP_BEFORE = timedelta(seconds=10)
CLIP_AFTER = timedelta(seconds=20)
OPEN_STATES = ("new", "acknowledged", "assigned")


class AlarmError(Exception):
    """An action that does not apply to the alarm in its current state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _record(db: sqlite3.Connection, alarm_id: str, actor_id: int | None, action: str, details: dict[str, Any]) -> None:
    stamp = _now()
    db.execute("INSERT INTO alarm_history(alarm_id,actor_id,action,details_json,occurred_at) VALUES(?,?,?,?,?)",
               (alarm_id, actor_id, action, json.dumps(details, separators=(",", ":"), sort_keys=True), stamp))
    db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
               (actor_id, f"alarm_{action}", "alarm", alarm_id, json.dumps(details, separators=(",", ":"), sort_keys=True)))


def _notify(db: sqlite3.Connection, alarm_id: str, event_type: str, actor_id: int | None) -> None:
    from .webhooks import enqueue_event

    alarm = db.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone()
    actor = db.execute("SELECT username FROM users WHERE id=?", (actor_id,)).fetchone() if actor_id else None
    assignee = db.execute("SELECT username FROM users WHERE id=?", (alarm["assigned_to"],)).fetchone() if alarm["assigned_to"] else None
    enqueue_event(db, event_type, {
        "event_id": str(uuid.uuid4()), "event_type": event_type, "occurred_at": _now(),
        "alarm_id": alarm_id, "state": alarm["state"], "alarm_time": alarm["occurred_at"],
        "rule_id": alarm["rule_id"], "rule_name": alarm["rule_name"], "condition_type": alarm["condition_type"],
        "camera_id": alarm["camera_id"], "track_id": alarm["track_id"], "label": alarm["label"],
        "actor": actor["username"] if actor else None, "assigned_to": assignee["username"] if assignee else None,
    })


LABEL_TEXT = {"person": "Person", "vehicle": "Vehicle", "covered": "Camera covered", "moved": "Camera moved", "defocused": "Camera defocused"}


def label_text(label: object) -> str:
    """How an alarm's label reads in a notification: an object class, or what happened to the camera."""
    return LABEL_TEXT.get(str(label), str(label or "object").capitalize())


def create_alarm(db: sqlite3.Connection, rule_event_id: str) -> str:
    """Raise the alarm for a rule event, inside the caller's transaction."""
    event = db.execute("SELECT * FROM rule_events WHERE id=?", (rule_event_id,)).fetchone()
    occurred = _parse(event["occurred_at"]).astimezone(timezone.utc)
    alarm_id = str(uuid.uuid4())
    stamp = _now()
    db.execute(
        "INSERT INTO alarms(id,rule_event_id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json,clip_starts_at,clip_ends_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (alarm_id, event["id"], event["rule_id"], event["rule_name"], event["camera_id"], event["track_id"], event["label"], event["condition_type"],
         occurred.isoformat(), event["box_json"], (occurred - CLIP_BEFORE).isoformat(), (occurred + CLIP_AFTER).isoformat(), stamp, stamp),
    )
    if db.execute("SELECT 1 FROM rules WHERE id=? AND pop_into_live=1", (event["rule_id"],)).fetchone():
        db.execute("INSERT INTO alarm_pop_ins(alarm_id) VALUES(?)", (alarm_id,))
    _record(db, alarm_id, None, "created", {"rule_name": event["rule_name"], "camera_id": event["camera_id"]})
    _notify(db, alarm_id, "alarm.created", None)
    from .push import queue_for_alarm
    from .rule_actions import queue_for_event
    queue_for_alarm(db, alarm_id)
    queue_for_event(db, event["id"], alarm_id)
    return alarm_id


def _open_alarm(db: sqlite3.Connection, alarm_id: str) -> sqlite3.Row:
    alarm = db.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone()
    if alarm is None:
        raise LookupError("Alarm not found")
    return alarm


def _change(alarm_id: str, apply) -> None:
    from .store import connect

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            apply(db, _open_alarm(db, alarm_id))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def acknowledge(alarm_id: str, actor_id: int) -> None:
    def apply(db: sqlite3.Connection, alarm: sqlite3.Row) -> None:
        if alarm["state"] != "new":
            raise AlarmError("Only a new alarm can be acknowledged")
        stamp = _now()
        db.execute("UPDATE alarms SET state='acknowledged',acknowledged_by=?,acknowledged_at=?,updated_at=? WHERE id=?", (actor_id, stamp, stamp, alarm_id))
        _record(db, alarm_id, actor_id, "acknowledged", {})
        _notify(db, alarm_id, "alarm.acknowledged", actor_id)
    _change(alarm_id, apply)


def assign(alarm_id: str, actor_id: int, assignee_id: int) -> None:
    def apply(db: sqlite3.Connection, alarm: sqlite3.Row) -> None:
        if alarm["state"] == "resolved":
            raise AlarmError("A resolved alarm cannot be assigned")
        assignee = db.execute("SELECT username FROM users WHERE id=?", (assignee_id,)).fetchone()
        if assignee is None:
            raise LookupError("User not found")
        stamp = _now()
        # Assigning someone also acknowledges an alarm nobody had acknowledged yet.
        db.execute("UPDATE alarms SET state='assigned',assigned_to=?,acknowledged_by=coalesce(acknowledged_by,?),acknowledged_at=coalesce(acknowledged_at,?),updated_at=? WHERE id=?",
                   (assignee_id, actor_id, stamp, stamp, alarm_id))
        _record(db, alarm_id, actor_id, "assigned", {"assigned_to": assignee["username"]})
        _notify(db, alarm_id, "alarm.assigned", actor_id)
    _change(alarm_id, apply)


def comment(alarm_id: str, actor_id: int, body: str) -> None:
    def apply(db: sqlite3.Connection, alarm: sqlite3.Row) -> None:
        db.execute("INSERT INTO alarm_comments(id,alarm_id,author_id,body,created_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), alarm_id, actor_id, body, _now()))
        db.execute("UPDATE alarms SET updated_at=? WHERE id=?", (_now(), alarm_id))
        _record(db, alarm_id, actor_id, "commented", {"length": len(body)})
    _change(alarm_id, apply)


def resolve(alarm_id: str, actor_id: int, note: str | None) -> None:
    def apply(db: sqlite3.Connection, alarm: sqlite3.Row) -> None:
        if alarm["state"] == "resolved":
            raise AlarmError("The alarm is already resolved")
        stamp = _now()
        if note:
            db.execute("INSERT INTO alarm_comments(id,alarm_id,author_id,body,created_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), alarm_id, actor_id, note, stamp))
        db.execute("UPDATE alarms SET state='resolved',resolved_by=?,resolved_at=?,updated_at=? WHERE id=?", (actor_id, stamp, stamp, alarm_id))
        _record(db, alarm_id, actor_id, "resolved", {"with_note": bool(note)})
        _notify(db, alarm_id, "alarm.resolved", actor_id)
    _change(alarm_id, apply)


def delete(alarm_id: str, actor_id: int) -> None:
    def apply(db: sqlite3.Connection, alarm: sqlite3.Row) -> None:
        db.execute("DELETE FROM alarms WHERE id=?", (alarm_id,))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (actor_id, "alarm_deleted", "alarm", alarm_id, json.dumps({"rule_name": alarm["rule_name"], "camera_id": alarm["camera_id"], "state": alarm["state"]})))
    _change(alarm_id, apply)


def resolved_footage_days(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT resolved_footage_days FROM alarm_settings WHERE singleton_id=1").fetchone()
    return int(row[0]) if row else 30


def resolved_kept_since(db: sqlite3.Connection, now: datetime) -> str:
    """Alarms resolved after this time still keep their footage."""
    return (now - timedelta(days=resolved_footage_days(db))).astimezone(timezone.utc).isoformat()


def protects_footage(db: sqlite3.Connection, camera_id: str, starts_at: str, ends_at: str, kept_since: str) -> bool:
    """Whether an open, or recently resolved, alarm's clip overlaps this recorded interval."""
    return db.execute(
        "SELECT 1 FROM alarms WHERE camera_id=? AND clip_starts_at<? AND clip_ends_at>? AND (resolved_at IS NULL OR resolved_at>?) LIMIT 1",
        (camera_id, ends_at, starts_at, kept_since),
    ).fetchone() is not None


def alarm_view(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    names = {user["id"]: user["username"] for user in db.execute(
        "SELECT id,username FROM users WHERE id IN (?,?,?)", (row["assigned_to"], row["acknowledged_by"], row["resolved_by"]))}
    camera = db.execute("SELECT name FROM cameras WHERE id=?", (row["camera_id"],)).fetchone()
    return {
        "id": row["id"], "state": row["state"], "occurred_at": row["occurred_at"], "rule_id": row["rule_id"], "rule_name": row["rule_name"],
        "condition_type": row["condition_type"], "camera_id": row["camera_id"], "camera_name": camera["name"] if camera else None,
        "track_id": row["track_id"], "label": row["label"], "box": json.loads(row["box_json"]),
        "assigned_to": names.get(row["assigned_to"]), "acknowledged_by": names.get(row["acknowledged_by"]), "acknowledged_at": row["acknowledged_at"],
        "resolved_by": names.get(row["resolved_by"]), "resolved_at": row["resolved_at"], "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def alarm_detail(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    camera_id = row["camera_id"]
    covering = db.execute(
        "SELECT id FROM recording_segments WHERE camera_id=? AND starts_at<=? AND ends_at>? AND unavailable_at IS NULL ORDER BY starts_at DESC LIMIT 1",
        (camera_id, row["occurred_at"], row["occurred_at"])).fetchone()
    segments = db.execute(
        "SELECT id,starts_at,ends_at FROM recording_segments WHERE camera_id=? AND starts_at<? AND ends_at>? AND unavailable_at IS NULL ORDER BY starts_at LIMIT 20",
        (camera_id, row["clip_ends_at"], row["clip_starts_at"])).fetchall()
    comments = db.execute(
        "SELECT c.id,c.body,c.created_at,u.username AS author FROM alarm_comments c LEFT JOIN users u ON u.id=c.author_id WHERE c.alarm_id=? ORDER BY c.created_at,c.id",
        (row["id"],)).fetchall()
    history = db.execute(
        "SELECT h.action,h.details_json,h.occurred_at,u.username AS actor FROM alarm_history h LEFT JOIN users u ON u.id=h.actor_id WHERE h.alarm_id=? ORDER BY h.id",
        (row["id"],)).fetchall()
    return {
        **alarm_view(db, row),
        "snapshot_url": (f"/api/cameras/{camera_id}/segments/{covering['id']}/snapshot?at={quote(row['occurred_at'])}" if covering else None),
        "clip": {
            "starts_at": row["clip_starts_at"], "ends_at": row["clip_ends_at"],
            "segments": [{"id": segment["id"], "starts_at": segment["starts_at"], "ends_at": segment["ends_at"],
                          "media_url": f"/api/cameras/{camera_id}/segments/{segment['id']}/media"} for segment in segments],
            "note": None if segments else "No recorded footage covers this alarm yet; footage appears once its recording is indexed.",
        },
        "comments": [dict(comment) for comment in comments],
        "history": [{"action": entry["action"], "actor": entry["actor"], "occurred_at": entry["occurred_at"], "details": json.loads(entry["details_json"])} for entry in history],
    }
