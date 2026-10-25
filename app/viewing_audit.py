"""Who watched which camera, from when to when, and every export, download and snapshot.

Live viewing and playback are sessions: a live session runs from the stream opening to it closing, and a
playback session from the first recording a person asks for to the last, ending after ``PLAYBACK_GAP`` without a
request. Exports, downloads and snapshots are single rows. Both reports group the same rows, so they agree.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal

from .store import connect

logger = logging.getLogger("nvr.viewing_audit")

PLAYBACK_GAP = timedelta(minutes=2)
HEARTBEAT_SECONDS = 30
RETENTION_DAYS = (7, 3650)
Kind = Literal["live", "playback", "export", "download", "snapshot"]
KINDS: tuple[str, ...] = ("live", "playback", "export", "download", "snapshot")
SESSION_KINDS = ("live", "playback")


def stamp(moment: datetime | None = None) -> str:
    """The one timestamp format of the table, so text comparison orders by time."""
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _who(db: sqlite3.Connection, user_id: int) -> str:
    row = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    return row["username"] if row else f"user {user_id}"


def _camera_name(db: sqlite3.Connection, camera_id: str) -> str:
    row = db.execute("SELECT name FROM cameras WHERE id=?", (camera_id,)).fetchone()
    return row["name"] if row else camera_id


def begin(db: sqlite3.Connection, user_id: int, camera_id: str, kind: Kind, quality: str | None = None) -> int:
    now = stamp()
    cursor = db.execute(
        "INSERT INTO viewing_audit(user_id,username,camera_id,camera_name,kind,started_at,last_seen_at,quality) VALUES(?,?,?,?,?,?,?,?)",
        (user_id, _who(db, user_id), camera_id, _camera_name(db, camera_id), kind, now, now, quality))
    return int(cursor.lastrowid)


def heartbeat(db: sqlite3.Connection, session_id: int) -> None:
    db.execute("UPDATE viewing_audit SET last_seen_at=? WHERE id=? AND ended_at IS NULL", (stamp(), session_id))


def end(db: sqlite3.Connection, session_id: int) -> None:
    now = stamp()
    db.execute("UPDATE viewing_audit SET ended_at=?,last_seen_at=? WHERE id=? AND ended_at IS NULL", (now, now, session_id))


def touch_playback(db: sqlite3.Connection, user_id: int, camera_id: str) -> None:
    """A recording was requested: extend this person's playback session on the camera, or start one."""
    now = datetime.now(timezone.utc)
    updated = db.execute(
        "UPDATE viewing_audit SET ended_at=?,last_seen_at=? WHERE id=(SELECT id FROM viewing_audit WHERE user_id=? AND camera_id=? AND kind='playback' "
        "AND ended_at>? ORDER BY id DESC LIMIT 1)", (stamp(now), stamp(now), user_id, camera_id, stamp(now - PLAYBACK_GAP)))
    if updated.rowcount:
        return
    session_id = begin(db, user_id, camera_id, "playback")
    db.execute("UPDATE viewing_audit SET ended_at=started_at WHERE id=?", (session_id,))


def record_events(db: sqlite3.Connection, user_id: int | None, camera_ids: Iterable[str], kind: Kind, reference: str | None = None) -> None:
    """One row per camera for an export, download or snapshot."""
    if user_id is None:
        return
    now = stamp()
    who = _who(db, user_id)
    db.executemany(
        "INSERT INTO viewing_audit(user_id,username,camera_id,camera_name,kind,started_at,ended_at,last_seen_at,reference) VALUES(?,?,?,?,?,?,?,?,?)",
        [(user_id, who, camera_id, _camera_name(db, camera_id), kind, now, now, now, reference) for camera_id in dict.fromkeys(camera_ids)])


def safely(action: Any, *args: Any, **kwargs: Any) -> Any:
    """Run an audit write on its own connection; a failure is logged, never allowed to cut off someone's viewing."""
    try:
        with connect() as db:
            return action(db, *args, **kwargs)
    except sqlite3.Error:
        logger.warning("The viewing audit could not be written", exc_info=True)
        return None


def close_orphans(db: sqlite3.Connection) -> None:
    """Sessions a crash left open ended when they were last seen alive."""
    db.execute("UPDATE viewing_audit SET ended_at=last_seen_at WHERE ended_at IS NULL")


def retention_days(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT audit_retention_days FROM security_settings WHERE singleton_id=1").fetchone()["audit_retention_days"])


def prune(db: sqlite3.Connection, now: datetime | None = None) -> int:
    """Remove entries older than the configured retention (by when they ended), and return how many."""
    cutoff = stamp((now or datetime.now(timezone.utc)) - timedelta(days=retention_days(db)))
    return db.execute("DELETE FROM viewing_audit WHERE COALESCE(ended_at,last_seen_at)<?", (cutoff,)).rowcount


# The seconds a row spends inside [start, end]; an entry without a duration counts only if it happened in the range.
_SECONDS = ("MAX(0, (julianday(MIN(COALESCE(ended_at,last_seen_at), :end)) - julianday(MAX(started_at, :start))) * 86400)")
_IN_RANGE = "(started_at<:end AND COALESCE(ended_at,last_seen_at)>=:start)"


def _filters(user_id: int | None, camera_id: str | None, kind: str | None) -> tuple[str, dict[str, Any]]:
    clauses, values = [], {}
    if user_id is not None:
        clauses.append("user_id=:user_id")
        values["user_id"] = user_id
    if camera_id:
        clauses.append("camera_id=:camera_id")
        values["camera_id"] = camera_id
    if kind:
        clauses.append("kind=:kind")
        values["kind"] = kind
    return "".join(f" AND {clause}" for clause in clauses), values


def _range(start: datetime, end: datetime) -> dict[str, Any]:
    return {"start": stamp(start), "end": stamp(end)}


def report(db: sqlite3.Connection, start: datetime, end: datetime, by: Literal["person", "camera"],
           user_id: int | None = None, camera_id: str | None = None) -> dict[str, Any]:
    """Totals per person or per camera over the range; the totals row is the same for both groupings."""
    extra, values = _filters(user_id, camera_id, None)
    values.update(_range(start, end))
    key = "COALESCE(CAST(user_id AS TEXT),'name:'||username)" if by == "person" else "camera_id"
    other = "camera_id" if by == "person" else "COALESCE(CAST(user_id AS TEXT),'name:'||username)"
    measures = (
        f"SUM(CASE WHEN kind='live' THEN 1 ELSE 0 END) AS live_sessions, SUM(CASE WHEN kind='live' THEN {_SECONDS} ELSE 0 END) AS live_seconds, "
        f"SUM(CASE WHEN kind='playback' THEN 1 ELSE 0 END) AS playback_sessions, SUM(CASE WHEN kind='playback' THEN {_SECONDS} ELSE 0 END) AS playback_seconds, "
        "SUM(CASE WHEN kind='export' THEN 1 ELSE 0 END) AS exports, SUM(CASE WHEN kind='download' THEN 1 ELSE 0 END) AS downloads, "
        "SUM(CASE WHEN kind='snapshot' THEN 1 ELSE 0 END) AS snapshots")
    where = f"WHERE {_IN_RANGE}{extra}"
    rows = db.execute(
        f"SELECT {key} AS key, MAX(user_id) AS user_id, MAX(username) AS username, MAX(camera_id) AS camera_id, MAX(camera_name) AS camera_name, "
        f"COUNT(DISTINCT {other}) AS distinct_other, MIN(started_at) AS first_at, MAX(COALESCE(ended_at,last_seen_at)) AS last_at, {measures} "
        f"FROM viewing_audit {where} GROUP BY {key} ORDER BY live_seconds+playback_seconds DESC, key", values).fetchall()
    total = db.execute(f"SELECT COUNT(*) AS entries, {measures} FROM viewing_audit {where}", values).fetchone()

    def shape(row: sqlite3.Row) -> dict[str, Any]:
        item: dict[str, Any] = {name: int(row[name] or 0) for name in ("live_sessions", "playback_sessions", "exports", "downloads", "snapshots")}
        item["live_seconds"] = round(row["live_seconds"] or 0, 1)
        item["playback_seconds"] = round(row["playback_seconds"] or 0, 1)
        return item

    people = by == "person"
    return {
        "by": by, "start": stamp(start), "end": stamp(end),
        "rows": [{
            **({"user_id": row["user_id"], "username": row["username"], "cameras": row["distinct_other"]} if people
               else {"camera_id": row["camera_id"], "camera_name": row["camera_name"], "people": row["distinct_other"]}),
            "first_at": row["first_at"], "last_at": row["last_at"], **shape(row),
        } for row in rows],
        "total": {"entries": int(total["entries"]), **shape(total)},
    }


def entries(db: sqlite3.Connection, start: datetime, end: datetime, user_id: int | None = None, camera_id: str | None = None,
            kind: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    extra, values = _filters(user_id, camera_id, kind)
    values.update(_range(start, end))
    where = f"WHERE {_IN_RANGE}{extra}"
    count = db.execute(f"SELECT COUNT(*) FROM viewing_audit {where}", values).fetchone()[0]
    rows = db.execute(
        f"SELECT id,user_id,username,camera_id,camera_name,kind,started_at,COALESCE(ended_at,last_seen_at) AS ended_at,ended_at IS NULL AS open,quality,reference, "
        f"CASE WHEN kind IN ('live','playback') THEN {_SECONDS} ELSE 0 END AS seconds FROM viewing_audit {where} ORDER BY started_at DESC,id DESC LIMIT :limit OFFSET :offset",
        {**values, "limit": limit, "offset": offset}).fetchall()
    return {"total": int(count), "entries": [{
        "id": row["id"], "user_id": row["user_id"], "username": row["username"], "camera_id": row["camera_id"], "camera_name": row["camera_name"],
        "kind": row["kind"], "started_at": row["started_at"], "ended_at": row["ended_at"] if row["kind"] in SESSION_KINDS else None,
        "open": bool(row["open"]) and row["kind"] == "live", "seconds": round(row["seconds"], 1), "quality": row["quality"], "reference": row["reference"],
    } for row in rows]}
