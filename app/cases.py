"""Case files (#129): everything about one incident, held and exported together.

A case has an owner, members and a status. People other than its members and administrators cannot tell it exists. Its items are clips (a
camera and a time range), marks, notes, alarms, saved searches and exports. The footage of every clip is held (``recording_holds`` rows
that carry the case's id) while the case is open, so the existing hold rules keep it from being deleted; removing a clip, or closing the
case after a confirmation, releases only the holds this case placed. Items on a camera a person may not see are not shown to them. Every
opening and change is recorded in ``case_audit``.

One evidence package covers the whole case: an export of each clip (with its own manifest and signed chain), the notes, the audit and a
manifest of the lot, which verifies offline. Packages are built by the exporter's poll loop (``advance_packages``) so a long export
does not depend on a browser staying open.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import access as access_rules, viewing_audit
from .store import connect

MAX_TITLE = 200
MAX_TEXT = 5000
MAX_CLIP = timedelta(hours=24)
MAX_ITEMS = 500
MAX_SEARCH_TRACKS = 200
OPEN, CLOSED = "open", "closed"
OPENED_AGAIN_AFTER = timedelta(minutes=10)
PACKAGE_DIRECTORY = "cases"


class CaseError(Exception):
    """A request the case cannot take (status code in ``status``)."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def audit(db: sqlite3.Connection, case_id: str, actor_id: int | None, action: str, **detail: Any) -> None:
    db.execute("INSERT INTO case_audit(case_id,actor_id,action,detail_json,created_at) VALUES(?,?,?,?,?)",
               (case_id, actor_id, action, json.dumps(detail, sort_keys=True), _iso(_now())))


def _username(db: sqlite3.Connection, user_id: int | None) -> str | None:
    row = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone() if user_id is not None else None
    return row["username"] if row else None


# --- who may open a case -----------------------------------------------------------------------------------------------------------

def is_member(db: sqlite3.Connection, case_id: str, user_id: int) -> bool:
    return db.execute("SELECT 1 FROM case_members WHERE case_id=? AND user_id=?", (case_id, user_id)).fetchone() is not None


def find(db: sqlite3.Connection, case_id: str, user: Any) -> sqlite3.Row | None:
    """The case if this person is a member or an administrator; otherwise None, which is what a case that does not exist gives."""
    row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if row is None:
        return None
    if user["role"] == "administrator" or is_member(db, case_id, int(user["id"])):
        return row
    return None


def is_manager(db: sqlite3.Connection, row: sqlite3.Row, user: Any) -> bool:
    """The owner and administrators may change who is in a case, rename it and close it."""
    return user["role"] == "administrator" or row["owner_id"] == int(user["id"])


def record_denied(db: sqlite3.Connection, case_id: str, user: Any) -> None:
    """A try at a case this person is not in, for the administrators' audit log (the case's own trail is for its members)."""
    db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
               (int(user["id"]), "case_access_denied", "case", case_id, "{}"))


def record_opened(db: sqlite3.Connection, case_id: str, user: Any) -> None:
    """Opening a case is audited, once for each stretch of use rather than for every refresh."""
    recent = db.execute("SELECT created_at FROM case_audit WHERE case_id=? AND actor_id=? AND action='opened' ORDER BY id DESC LIMIT 1",
                        (case_id, int(user["id"]))).fetchone()
    if recent is None or _now() - _parse(recent["created_at"]) >= OPENED_AGAIN_AFTER:
        audit(db, case_id, int(user["id"]), "opened")


# --- cases -------------------------------------------------------------------------------------------------------------------------

def create_case(db: sqlite3.Connection, owner_id: int, title: str, description: str = "") -> str:
    title, description = title.strip(), description.strip()
    if not title or len(title) > MAX_TITLE:
        raise CaseError(f"Give the case a title of up to {MAX_TITLE} characters")
    if len(description) > MAX_TEXT:
        raise CaseError(f"The description is limited to {MAX_TEXT} characters")
    case_id = str(uuid.uuid4())
    now = _iso(_now())
    db.execute("INSERT INTO cases(id,title,description,owner_id,created_at) VALUES(?,?,?,?,?)", (case_id, title, description, owner_id, now))
    db.execute("INSERT INTO case_members(case_id,user_id,added_by,added_at) VALUES(?,?,?,?)", (case_id, owner_id, owner_id, now))
    audit(db, case_id, owner_id, "created", title=title)
    return case_id


def _counts(db: sqlite3.Connection, case_id: str) -> dict[str, int]:
    counts = {row["kind"]: row["n"] for row in db.execute("SELECT kind,COUNT(*) AS n FROM case_items WHERE case_id=? AND removed_at IS NULL GROUP BY kind", (case_id,))}
    return {kind: counts.get(kind, 0) for kind in ("clip", "mark", "note", "alarm", "search", "export")}


def summary(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "title": row["title"], "status": row["status"], "owner": _username(db, row["owner_id"]), "owner_id": row["owner_id"],
            "created_at": row["created_at"], "closed_at": row["closed_at"], "counts": _counts(db, row["id"])}


def list_cases(db: sqlite3.Connection, user: Any, status: str | None = None) -> list[dict[str, Any]]:
    clauses, values = [], []
    if user["role"] != "administrator":
        clauses.append("id IN (SELECT case_id FROM case_members WHERE user_id=?)")
        values.append(int(user["id"]))
    if status in (OPEN, CLOSED):
        clauses.append("status=?")
        values.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return [summary(db, row) for row in db.execute(f"SELECT * FROM cases {where} ORDER BY created_at DESC LIMIT 200", values)]


def _item_cameras(row: sqlite3.Row) -> list[str]:
    if row["kind"] == "export":
        return list(json.loads(row["data_json"]).get("camera_ids", []))
    return [row["camera_id"]] if row["camera_id"] else []


def item_view(db: sqlite3.Connection, row: sqlite3.Row, user: Any) -> dict[str, Any]:
    """An item, or just that it exists when it concerns a camera this person may not see."""
    if not all(user.access.can_see(camera_id) for camera_id in _item_cameras(row)):
        return {"id": row["id"], "kind": row["kind"], "restricted": True, "added_at": row["added_at"]}
    view = {"id": row["id"], "kind": row["kind"], "restricted": False, "camera_id": row["camera_id"], "starts_at": row["starts_at"], "ends_at": row["ends_at"],
            "ref": row["ref"], "text": row["text"], "data": json.loads(row["data_json"]), "added_by": _username(db, row["added_by"]), "added_at": row["added_at"]}
    if row["camera_id"]:
        camera = db.execute("SELECT name FROM cameras WHERE id=?", (row["camera_id"],)).fetchone()
        view["camera_name"] = camera["name"] if camera else None
    return view


def held_segment_ids(db: sqlite3.Connection, case_id: str) -> set[int]:
    return {row[0] for row in db.execute("SELECT segment_id FROM recording_holds WHERE case_id=? AND released_at IS NULL AND segment_id IS NOT NULL", (case_id,))}


def detail(db: sqlite3.Connection, row: sqlite3.Row, user: Any) -> dict[str, Any]:
    items = [item_view(db, item, user) for item in db.execute("SELECT * FROM case_items WHERE case_id=? AND removed_at IS NULL ORDER BY added_at,rowid", (row["id"],))]
    members = [{"user_id": member["user_id"], "username": member["username"], "added_at": member["added_at"], "owner": member["user_id"] == row["owner_id"]}
               for member in db.execute("SELECT m.user_id,u.username,m.added_at FROM case_members m JOIN users u ON u.id=m.user_id WHERE m.case_id=? ORDER BY m.added_at", (row["id"],))]
    packages = [package_view(package) for package in db.execute("SELECT * FROM case_packages WHERE case_id=? ORDER BY created_at DESC LIMIT 20", (row["id"],))]
    return {**summary(db, row), "description": row["description"], "items": items, "restricted_items": sum(1 for item in items if item["restricted"]),
            "members": members, "held_segments": len(held_segment_ids(db, row["id"])), "packages": packages,
            "can_manage": is_manager(db, row, user)}


def update_case(db: sqlite3.Connection, row: sqlite3.Row, actor_id: int, title: str | None, description: str | None) -> None:
    if title is not None:
        title = title.strip()
        if not title or len(title) > MAX_TITLE:
            raise CaseError(f"Give the case a title of up to {MAX_TITLE} characters")
        db.execute("UPDATE cases SET title=? WHERE id=?", (title, row["id"]))
    if description is not None:
        if len(description.strip()) > MAX_TEXT:
            raise CaseError(f"The description is limited to {MAX_TEXT} characters")
        db.execute("UPDATE cases SET description=? WHERE id=?", (description.strip(), row["id"]))
    audit(db, row["id"], actor_id, "updated", title=title is not None, description=description is not None)


# --- members -----------------------------------------------------------------------------------------------------------------------

def add_member(db: sqlite3.Connection, row: sqlite3.Row, actor_id: int, user_id: int) -> None:
    if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
        raise CaseError("No such user", 404)
    if is_member(db, row["id"], user_id):
        raise CaseError("Already a member of this case", 409)
    db.execute("INSERT INTO case_members(case_id,user_id,added_by,added_at) VALUES(?,?,?,?)", (row["id"], user_id, actor_id, _iso(_now())))
    audit(db, row["id"], actor_id, "member_added", user=_username(db, user_id))


def remove_member(db: sqlite3.Connection, row: sqlite3.Row, actor_id: int, user_id: int) -> None:
    if user_id == row["owner_id"]:
        raise CaseError("The owner stays in the case", 409)
    if db.execute("DELETE FROM case_members WHERE case_id=? AND user_id=?", (row["id"], user_id)).rowcount != 1:
        raise CaseError("Not a member of this case", 404)
    audit(db, row["id"], actor_id, "member_removed", user=_username(db, user_id))


# --- holds -------------------------------------------------------------------------------------------------------------------------

def _available_segments(db: sqlite3.Connection, camera_id: str, start: datetime, end: datetime) -> list[sqlite3.Row]:
    return db.execute("SELECT id,starts_at,ends_at FROM recording_segments WHERE camera_id=? AND starts_at<? AND ends_at>? AND unavailable_at IS NULL ORDER BY starts_at",
                      (camera_id, _iso(end), _iso(start))).fetchall()


def hold_segments(db: sqlite3.Connection, case_id: str, camera_id: str, segments: list[sqlite3.Row], actor_id: int) -> int:
    """Hold these segments for the case (a hold of its own, alongside any other); how many were not already held for it."""
    held = held_segment_ids(db, case_id)
    added = 0
    for segment in segments:
        if segment["id"] in held:
            continue
        db.execute("INSERT INTO recording_holds(id,segment_id,camera_id,created_by,case_id) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), segment["id"], camera_id, actor_id, case_id))
        added += 1
    return added


def release_holds(db: sqlite3.Connection, case_id: str, actor_id: int, keep: set[int] | None = None) -> int:
    """Let go of the holds this case placed (all of them, or all but those on ``keep``). Holds placed by hand or by other cases stay."""
    keep = keep or set()
    rows = db.execute("SELECT id,segment_id FROM recording_holds WHERE case_id=? AND released_at IS NULL", (case_id,)).fetchall()
    released = 0
    for hold in rows:
        if hold["segment_id"] in keep:
            continue
        db.execute("UPDATE recording_holds SET released_by=?,released_at=? WHERE id=?", (actor_id, _iso(_now()), hold["id"]))
        released += 1
    return released


# --- items -------------------------------------------------------------------------------------------------------------------------

def _open_case(row: sqlite3.Row) -> None:
    if row["status"] != OPEN:
        raise CaseError("This case is closed", 409)


def _room(db: sqlite3.Connection, case_id: str) -> None:
    if db.execute("SELECT COUNT(*) FROM case_items WHERE case_id=? AND removed_at IS NULL", (case_id,)).fetchone()[0] >= MAX_ITEMS:
        raise CaseError(f"A case holds up to {MAX_ITEMS} items", 409)


def _camera(db: sqlite3.Connection, user: Any, camera_id: str) -> sqlite3.Row:
    camera = db.execute("SELECT id,name FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if camera is None or not user.access.can_see(camera_id):
        raise CaseError("Camera not found", 404)  # A camera this person may not see is just as missing
    return camera


def _insert(db: sqlite3.Connection, case_id: str, actor_id: int, kind: str, *, camera_id: str | None = None, starts_at: str | None = None, ends_at: str | None = None,
            ref: str | None = None, text: str = "", data: dict[str, Any] | None = None) -> str:
    item_id = str(uuid.uuid4())
    db.execute("INSERT INTO case_items(id,case_id,kind,camera_id,starts_at,ends_at,ref,text,data_json,added_by,added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (item_id, case_id, kind, camera_id, starts_at, ends_at, ref, text, json.dumps(data or {}, sort_keys=True), actor_id, _iso(_now())))
    return item_id


def _covered_seconds(segments: list[sqlite3.Row], start: datetime, end: datetime) -> float:
    spans = sorted((max(start, _parse(item["starts_at"])), min(end, _parse(item["ends_at"]))) for item in segments)
    total, cursor = 0.0, start
    for begin, finish in spans:
        begin = max(begin, cursor)
        if finish > begin:
            total += (finish - begin).total_seconds()
            cursor = finish
    return total


def add_clip(db: sqlite3.Connection, row: sqlite3.Row, user: Any, camera_id: str, start: datetime, end: datetime, label: str = "") -> str:
    """A camera and a time range of recorded footage, which is held for as long as the case is open and the clip stays in it."""
    _open_case(row)
    _room(db, row["id"])
    camera = _camera(db, user, camera_id)
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise CaseError("A clip needs a start and an end with a time zone, the end after the start")
    if end - start > MAX_CLIP:
        raise CaseError("A clip is at most 24 hours; add several for a longer stretch")
    if end > _now() + timedelta(seconds=5):
        raise CaseError("A clip must be of footage already recorded")
    segments = _available_segments(db, camera_id, start, end)
    if not segments:
        raise CaseError("No recorded footage is available in that range")
    covered = _covered_seconds(segments, start, end)
    held = hold_segments(db, row["id"], camera_id, segments, int(user["id"]))
    item = _insert(db, row["id"], int(user["id"]), "clip", camera_id=camera_id, starts_at=_iso(start), ends_at=_iso(end), text=label.strip()[:200],
                   data={"segments": len(segments), "covered_seconds": round(covered, 1), "uncovered_seconds": round((end - start).total_seconds() - covered, 1)})
    audit(db, row["id"], int(user["id"]), "clip_added", item=item, camera=camera["name"], start=_iso(start), end=_iso(end), segments_held=held)
    return item


def add_mark(db: sqlite3.Connection, row: sqlite3.Row, user: Any, camera_id: str, at: datetime, text: str) -> str:
    _open_case(row)
    _room(db, row["id"])
    camera = _camera(db, user, camera_id)
    if at.tzinfo is None:
        raise CaseError("A mark needs a time with a time zone")
    text = text.strip()
    if not text or len(text) > 500:
        raise CaseError("Say what the mark is, in up to 500 characters")
    item = _insert(db, row["id"], int(user["id"]), "mark", camera_id=camera_id, starts_at=_iso(at), text=text)
    audit(db, row["id"], int(user["id"]), "mark_added", item=item, camera=camera["name"], at=_iso(at))
    return item


def add_note(db: sqlite3.Connection, row: sqlite3.Row, user: Any, text: str) -> str:
    """Notes are only ever added: what was written, by whom and when stays as it was."""
    _open_case(row)
    _room(db, row["id"])
    text = text.strip()
    if not text or len(text) > MAX_TEXT:
        raise CaseError(f"A note has 1 to {MAX_TEXT} characters")
    item = _insert(db, row["id"], int(user["id"]), "note", text=text)
    audit(db, row["id"], int(user["id"]), "note_added", item=item)
    return item


def add_alarm(db: sqlite3.Connection, row: sqlite3.Row, user: Any, alarm_id: str) -> str:
    _open_case(row)
    _room(db, row["id"])
    alarm = db.execute("SELECT id,camera_id,rule_name,occurred_at,label FROM alarms WHERE id=?", (alarm_id,)).fetchone()
    if alarm is None or not user.access.can_see(alarm["camera_id"]):
        raise CaseError("Alarm not found", 404)
    item = _insert(db, row["id"], int(user["id"]), "alarm", camera_id=alarm["camera_id"], starts_at=alarm["occurred_at"], ref=alarm_id, text=alarm["rule_name"],
                   data={"label": alarm["label"]})
    audit(db, row["id"], int(user["id"]), "alarm_added", item=item, alarm=alarm_id)
    return item


def add_search(db: sqlite3.Connection, row: sqlite3.Row, user: Any, title: str, params: dict[str, Any], track_ids: list[str]) -> str:
    """A saved search: what was asked and the objects it found, of the cameras this person may see."""
    _open_case(row)
    _room(db, row["id"])
    title = title.strip()[:200]
    if not title:
        raise CaseError("Give the search a name")
    if len(json.dumps(params)) > 2000 or not all(isinstance(key, str) and isinstance(value, (str, int, float, bool, list, type(None))) for key, value in params.items()):
        raise CaseError("The search parameters are too large or not simple values")
    track_ids = list(dict.fromkeys(track_ids))[:MAX_SEARCH_TRACKS]
    seen = set()
    for start in range(0, len(track_ids), 500):
        part = track_ids[start:start + 500]
        for found in db.execute(f"SELECT id,camera_id FROM ai_tracks WHERE id IN ({','.join('?' * len(part))})", part):
            if user.access.can_see(found["camera_id"]):
                seen.add(found["id"])
    visible = [track for track in track_ids if track in seen]  # In the order the search listed them
    item = _insert(db, row["id"], int(user["id"]), "search", text=title, data={"params": params, "track_ids": visible})
    audit(db, row["id"], int(user["id"]), "search_added", item=item, results=len(visible))
    return item


def add_export(db: sqlite3.Connection, row: sqlite3.Row, user: Any, export_id: str) -> str:
    _open_case(row)
    _room(db, row["id"])
    export = db.execute("SELECT * FROM exports WHERE id=?", (export_id,)).fetchone()
    cameras = json.loads(export["camera_ids_json"]) if export else []
    if export is None or (user["role"] != "administrator" and export["requested_by"] != int(user["id"])) or not all(user.access.can_see(camera_id) for camera_id in cameras):
        raise CaseError("Export not found", 404)
    item = _insert(db, row["id"], int(user["id"]), "export", ref=export_id, starts_at=export["requested_start"], ends_at=export["requested_end"],
                   data={"camera_ids": cameras, "status": export["status"]})
    audit(db, row["id"], int(user["id"]), "export_added", item=item, export=export_id)
    return item


def remove_item(db: sqlite3.Connection, row: sqlite3.Row, user: Any, item_id: str) -> None:
    _open_case(row)
    item = db.execute("SELECT * FROM case_items WHERE id=? AND case_id=? AND removed_at IS NULL", (item_id, row["id"])).fetchone()
    if item is None or not all(user.access.can_see(camera_id) for camera_id in _item_cameras(item)):
        raise CaseError("Item not found", 404)
    if item["kind"] == "note":
        raise CaseError("A note stays in the case: add another to correct it", 409)
    db.execute("UPDATE case_items SET removed_at=?,removed_by=? WHERE id=?", (_iso(_now()), int(user["id"]), item_id))
    released = 0
    if item["kind"] == "clip":
        needed: set[int] = set()
        for clip in db.execute("SELECT * FROM case_items WHERE case_id=? AND kind='clip' AND removed_at IS NULL", (row["id"],)):
            needed |= {segment["id"] for segment in _available_segments(db, clip["camera_id"], _parse(clip["starts_at"]), _parse(clip["ends_at"]))}
        released = release_holds(db, row["id"], int(user["id"]), keep=needed)
    audit(db, row["id"], int(user["id"]), "item_removed", item=item_id, kind=item["kind"], holds_released=released)


def close_case(db: sqlite3.Connection, row: sqlite3.Row, actor_id: int, confirmed: bool) -> int:
    """Close the case and let its footage go back to normal retention. Needs a confirmation naming what is released; returns the holds released."""
    _open_case(row)
    held = len(held_segment_ids(db, row["id"]))
    if not confirmed:
        raise CaseError(f"Closing releases the hold on {held} recorded interval{'s' if held != 1 else ''}: footage not held by anything else becomes eligible for deletion under its retention policy. Confirm to close.", 409)
    released = release_holds(db, row["id"], actor_id)
    db.execute("UPDATE cases SET status='closed',closed_at=?,closed_by=? WHERE id=?", (_iso(_now()), actor_id, row["id"]))
    audit(db, row["id"], actor_id, "closed", holds_released=released)
    return released


# --- audit ---------------------------------------------------------------------------------------------------------------------------

def audit_extract(db: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    return [{"at": row["created_at"], "actor": _username(db, row["actor_id"]), "action": row["action"], "detail": json.loads(row["detail_json"])}
            for row in db.execute("SELECT * FROM case_audit WHERE case_id=? ORDER BY id", (case_id,))]


# --- packages ------------------------------------------------------------------------------------------------------------------------

def package_view(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "status": row["status"], "error": row["error"], "omitted_items": row["omitted"], "created_at": row["created_at"],
            "completed_at": row["completed_at"], "size_bytes": row["size_bytes"], "sha256": row["sha256"]}


def _clips_for(db: sqlite3.Connection, case_id: str, access: access_rules.Access) -> tuple[list[sqlite3.Row], int]:
    """The case's clips this person may see, and how many they may not."""
    clips = db.execute("SELECT * FROM case_items WHERE case_id=? AND kind='clip' AND removed_at IS NULL ORDER BY added_at,rowid", (case_id,)).fetchall()
    visible = [clip for clip in clips if access.can_see(clip["camera_id"])]
    return visible, len(clips) - len(visible)


def request_package(db: sqlite3.Connection, row: sqlite3.Row, user: Any) -> str:
    """Start an evidence package of the clips this person may see. It is built in the background, from an export of each clip."""
    clips, omitted = _clips_for(db, row["id"], user.access)
    if not clips:
        raise CaseError("This case has no clips you may export", 422)
    if db.execute("SELECT 1 FROM case_packages WHERE case_id=? AND status='building'", (row["id"],)).fetchone():
        raise CaseError("A package for this case is already being built", 409)
    package_id = str(uuid.uuid4())
    db.execute("INSERT INTO case_packages(id,case_id,requested_by,omitted,created_at) VALUES(?,?,?,?,?)", (package_id, row["id"], int(user["id"]), omitted, _iso(_now())))
    db.executemany("INSERT INTO case_package_clips(package_id,item_id) VALUES(?,?)", ((package_id, clip["id"]) for clip in clips))
    audit(db, row["id"], int(user["id"]), "package_requested", package=package_id, clips=len(clips), omitted=omitted)
    return package_id


def _fail(db: sqlite3.Connection, package: sqlite3.Row, reason: str) -> None:
    db.execute("UPDATE case_packages SET status='failed',error=?,completed_at=? WHERE id=?", (reason, _iso(_now()), package["id"]))
    audit(db, package["case_id"], package["requested_by"], "package_failed", package=package["id"], reason=reason)


def advance_packages() -> None:
    """Move every package being built one step: start the exports of its clips as the export queue has room, and when all are done, assemble it."""
    from .exports import create_export_record
    with connect() as db:
        packages = db.execute("SELECT * FROM case_packages WHERE status='building' ORDER BY created_at").fetchall()
    for package in packages:
        try:
            with connect() as db:
                _advance(db, package, create_export_record)
        except Exception as exc:  # noqa: BLE001 - a package that cannot be built is failed, not retried for ever
            with connect() as db:
                _fail(db, package, f"The package could not be built: {str(exc)[:200]}")


def _advance(db: sqlite3.Connection, package: sqlite3.Row, create_export_record: Any) -> None:
    rows = db.execute("SELECT p.item_id,p.export_id,i.camera_id,i.starts_at,i.ends_at FROM case_package_clips p JOIN case_items i ON i.id=p.item_id WHERE p.package_id=? ORDER BY i.added_at,i.rowid",
                      (package["id"],)).fetchall()
    for clip in rows:
        if clip["export_id"] is None:
            try:
                export_id = create_export_record(int(package["requested_by"]), [clip["camera_id"]], _parse(clip["starts_at"]), _parse(clip["ends_at"]), ["individual"])
            except OverflowError:
                return  # The export queue is full: the next poll tries again
            except (LookupError, ValueError) as exc:
                _fail(db, package, f"A clip could not be exported: {exc}")
                return
            db.execute("UPDATE case_package_clips SET export_id=? WHERE package_id=? AND item_id=?", (export_id, package["id"], clip["item_id"]))
    states = [db.execute("SELECT e.status,e.error FROM case_package_clips p LEFT JOIN exports e ON e.id=p.export_id WHERE p.package_id=? AND p.item_id=?", (package["id"], clip["item_id"])).fetchone()
              for clip in rows]
    for state in states:
        if state is None or state["status"] is None or state["status"] in ("failed", "cancelled"):
            _fail(db, package, f"A clip's export {'was cancelled' if state and state['status'] == 'cancelled' else 'failed'}" + (f": {state['error']}" if state and state["error"] else ""))
            return
    if all(state["status"] == "completed" for state in states):
        assemble(db, package)


def _case_json(db: sqlite3.Connection, case: sqlite3.Row, package: sqlite3.Row, access: access_rules.Access, exports_by_item: dict[str, str]) -> tuple[dict[str, Any], int]:
    items, omitted = [], 0
    for item in db.execute("SELECT * FROM case_items WHERE case_id=? AND removed_at IS NULL ORDER BY added_at,rowid", (case["id"],)):
        if not all(access.can_see(camera_id) for camera_id in _item_cameras(item)):
            omitted += 1
            continue
        entry = {"id": item["id"], "kind": item["kind"], "camera_id": item["camera_id"], "starts_at": item["starts_at"], "ends_at": item["ends_at"], "ref": item["ref"],
                 "text": item["text"], "data": json.loads(item["data_json"]), "added_by": _username(db, item["added_by"]), "added_at": item["added_at"]}
        if item["kind"] == "clip":
            entry["package_file"] = None  # Filled in as the files are written
            entry["export_id"] = exports_by_item.get(item["id"])
        items.append(entry)
    members = [row["username"] for row in db.execute("SELECT u.username FROM case_members m JOIN users u ON u.id=m.user_id WHERE m.case_id=? ORDER BY m.added_at", (case["id"],))]
    return {"case_id": case["id"], "title": case["title"], "description": case["description"], "status": case["status"], "owner": _username(db, case["owner_id"]),
            "created_at": case["created_at"], "closed_at": case["closed_at"], "members": members, "items": items}, omitted


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assemble(db: sqlite3.Connection, package: sqlite3.Row) -> None:
    """Write the case's evidence package: each clip's export, the notes, the audit and a manifest of every file."""
    from .recording import recordings_path
    case = db.execute("SELECT * FROM cases WHERE id=?", (package["case_id"],)).fetchone()
    requester = db.execute("SELECT id,role FROM users WHERE id=?", (package["requested_by"],)).fetchone()
    if case is None or requester is None:
        _fail(db, package, "The case or the person who asked for the package no longer exists")
        return
    access = access_rules.resolve(db, int(requester["id"]), requester["role"])
    mapping = {row["item_id"]: row["export_id"] for row in db.execute("SELECT item_id,export_id FROM case_package_clips WHERE package_id=?", (package["id"],))}
    document, omitted = _case_json(db, case, package, access, mapping)
    root = recordings_path().resolve()
    sources: dict[str, Path] = {}
    for entry in document["items"]:
        if entry["kind"] != "clip":
            continue
        export = db.execute("SELECT output_relative_path FROM exports WHERE id=?", (entry["export_id"],)).fetchone()
        path = (root / export["output_relative_path"]).resolve() if export and export["output_relative_path"] else None
        if path is None or root not in path.parents or not path.is_file():
            _fail(db, package, "A clip's export file is no longer available")
            return
        name = f"clips/{len(sources) + 1:02d}-{entry['export_id']}.zip"
        entry["package_file"] = name
        sources[name] = path
    notes = [entry for entry in document["items"] if entry["kind"] == "note"]
    notes_markdown = "".join(f"## {entry['added_at']} {entry['added_by'] or ''}\n\n{entry['text']}\n\n" for entry in notes) or "No notes.\n"
    generated = _iso(_now())
    metadata = {"case_id": case["id"], "package_id": package["id"], "generated_at": generated, "generated_by": _username(db, requester["id"]), "omitted_items": omitted,
                "audit_reference": {"resource_type": "case", "resource_id": case["id"]}}
    documents = {
        "case.json": json.dumps(document, indent=2, sort_keys=True).encode(),
        "notes.json": json.dumps(notes, indent=2, sort_keys=True).encode(),
        "notes.md": notes_markdown.encode(),
        "audit.json": json.dumps(audit_extract(db, case["id"]), indent=2, sort_keys=True).encode(),
        "metadata.json": json.dumps(metadata, indent=2, sort_keys=True).encode(),
    }
    # The case document names each clip's file, so it is written last of the documents that name files (above) and hashed as written.
    directory = root / PACKAGE_DIRECTORY / case["id"]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{package['id']}.zip"
    temporary = target.with_suffix(".partial")
    files = []
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as archive:
            for name, content in documents.items():
                archive.writestr(name, content)
                files.append({"path": name, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
            for name, source in sources.items():
                archive.write(source, name)
                files.append({"path": name, "size_bytes": source.stat().st_size, "sha256": _sha256_file(source)})
            archive.writestr("integrity.json", json.dumps({"algorithm": "sha256", "files": files}, indent=2, sort_keys=True))
        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        _fail(db, package, f"The package could not be written: {str(exc)[:160]}")
        return
    db.execute("UPDATE case_packages SET status='ready',relative_path=?,size_bytes=?,sha256=?,completed_at=? WHERE id=?",
               (str(target.relative_to(root)), target.stat().st_size, _sha256_file(target), _iso(_now()), package["id"]))
    audit(db, case["id"], int(requester["id"]), "package_ready", package=package["id"], clips=len(sources), omitted=omitted)


def package_path(row: sqlite3.Row) -> Path:
    from .recording import recordings_path
    if row["status"] != "ready" or not row["relative_path"]:
        raise FileNotFoundError("The package is not ready")
    root = recordings_path().resolve()
    path = (root / row["relative_path"]).resolve()
    if root not in path.parents:
        raise ValueError("The package path is invalid")
    return path


def verify_package_bytes(content: bytes, case_id: str, package_id: str, installation_key: str | None = None) -> dict[str, Any]:
    """Check an uploaded case package offline: every file against its manifest, and every clip's own evidence package, in full.

    ``complete`` says the case's clips are all there (a package of someone with less access can be valid and not complete)."""
    from .exports import MAX_VERIFY_PACKAGE_BYTES, verify_evidence_bytes
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or any(name.startswith("/") or ".." in Path(name).parts for name in names):
                raise ValueError("invalid archive paths")
            if sum(item.file_size for item in infos) > MAX_VERIFY_PACKAGE_BYTES:
                raise ValueError("uncompressed package exceeds the verification size limit")
            if names.count("integrity.json") != 1:
                raise ValueError("integrity manifest is missing or duplicated")
            manifest = json.loads(archive.read("integrity.json"))
            if manifest.get("algorithm") != "sha256" or not isinstance(manifest.get("files"), list):
                raise ValueError("unsupported integrity manifest")
            expected = {entry["path"]: entry for entry in manifest["files"]}
            delivered = set(names) - {"integrity.json"}
            if len(expected) != len(manifest["files"]) or delivered != set(expected):
                return {"checksums_valid": False, "files": [], "clips": [], "complete": False, "error": "package contents do not match the integrity manifest"}
            metadata = json.loads(archive.read("metadata.json"))
            if metadata.get("case_id") != case_id or metadata.get("package_id") != package_id or metadata.get("audit_reference") != {"resource_type": "case", "resource_id": case_id}:
                return {"checksums_valid": False, "files": [], "clips": [], "complete": False, "error": "package identity does not match the requested package"}
            files, valid = [], True
            for name in sorted(delivered):
                body = archive.read(name)
                matches = len(body) == expected[name].get("size_bytes") and hashlib.sha256(body).hexdigest() == expected[name].get("sha256")
                valid = valid and matches
                files.append({"path": name, "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "matches": matches})
            document = json.loads(archive.read("case.json"))
            clips, every_clip = [], True
            for entry in (item for item in document.get("items", []) if item.get("kind") == "clip"):
                name = entry.get("package_file")
                if not name or name not in delivered:
                    clips.append({"item_id": entry.get("id"), "export_id": entry.get("export_id"), "present": False, "valid": False, "error": "the clip's file is missing from the package"})
                    every_clip = False
                    continue
                inner = verify_evidence_bytes(archive.read(name), entry.get("export_id"), installation_key)
                inner_valid = bool(inner["checksums_valid"]) and (inner.get("chain") is None or bool(inner["chain"]["valid"]))
                every_clip = every_clip and inner_valid
                clips.append({"item_id": entry.get("id"), "export_id": entry.get("export_id"), "present": True, "valid": inner_valid, "error": inner.get("error"),
                              "chain": None if inner.get("chain") is None else ("valid" if inner["chain"]["valid"] else "invalid")})
            notes_present = {"notes.json", "notes.md", "audit.json"} <= delivered
            return {"checksums_valid": valid, "files": files, "clips": clips, "clips_valid": every_clip, "notes_and_audit_present": notes_present,
                    "omitted_items": metadata.get("omitted_items", 0), "complete": valid and every_clip and notes_present and metadata.get("omitted_items", 0) == 0,
                    "error": None if valid else "one or more file checksums do not match"}
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"checksums_valid": False, "files": [], "clips": [], "complete": False, "error": f"invalid case package: {str(exc)[:200]}"}


def package_cameras(db: sqlite3.Connection, package_id: str) -> list[str]:
    return [row[0] for row in db.execute("SELECT DISTINCT i.camera_id FROM case_package_clips p JOIN case_items i ON i.id=p.item_id WHERE p.package_id=?", (package_id,))]


def record_download(db: sqlite3.Connection, user: Any, package_id: str, case_id: str) -> None:
    cameras = package_cameras(db, package_id)
    viewing_audit.record_events(db, int(user["id"]), cameras, "download", package_id)
    audit(db, case_id, int(user["id"]), "package_downloaded", package=package_id)
