"""Retention by purpose and generated notices (#139).

``settings`` holds the details an administrator enters once (organisation, contact, whether a purpose is required). A *notice* (the signage text) and a
*summary* (the data-protection overview) are built from the live configuration each time they are asked for, so a change to a camera's audio or a policy's
retention shows at once. They name cameras, never their addresses or credentials, and are rendered as printable HTML pages as well as data.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from . import access
from .retention import DEFAULT_RETENTION_DAYS, effective_purpose, effective_retention

MAX_TEXT = 300
SETTING_FIELDS = ("default_purpose", "default_legal_basis", "organisation", "contact_name", "contact_email", "contact_phone")


class NoticeError(ValueError):
    """The settings or a policy cannot be saved as given."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def settings(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute("SELECT * FROM privacy_settings WHERE singleton_id=1").fetchone()
    return {"require_purpose": bool(row["require_purpose"]), **{field: row[field] for field in SETTING_FIELDS}, "updated_at": row["updated_at"]}


def save_settings(db: sqlite3.Connection, fields: dict[str, Any]) -> None:
    """Change the settings inside the caller's transaction; a field left out keeps its value."""
    current = settings(db)
    merged: dict[str, Any] = {"require_purpose": bool(fields.get("require_purpose", current["require_purpose"]))}
    for field in SETTING_FIELDS:
        merged[field] = str(fields.get(field, current[field]) or "").strip()
        if len(merged[field]) > MAX_TEXT:
            raise NoticeError(f"Keep each detail to {MAX_TEXT} characters")
    if merged["require_purpose"] and "default_purpose" in fields and not merged["default_purpose"]:
        raise NoticeError("The site default policy needs a purpose while a purpose is required")
    if merged["contact_email"] and ("@" not in merged["contact_email"] or " " in merged["contact_email"]):
        raise NoticeError("Enter a contact email address such as privacy@example.com")
    db.execute("UPDATE privacy_settings SET require_purpose=?,default_purpose=?,default_legal_basis=?,organisation=?,contact_name=?,contact_email=?,contact_phone=?,updated_at=? WHERE singleton_id=1",
               (int(merged["require_purpose"]), *(merged[field] for field in SETTING_FIELDS), _now().isoformat()))


def check_policy_text(db: sqlite3.Connection, purpose: str, legal_basis: str) -> tuple[str, str]:
    """A policy's purpose and legal basis as they will be stored; a purpose is required once the administrator has asked for that."""
    purpose, legal_basis = purpose.strip(), legal_basis.strip()
    if len(purpose) > MAX_TEXT or len(legal_basis) > MAX_TEXT:
        raise NoticeError(f"Keep the purpose and legal basis to {MAX_TEXT} characters each")
    if settings(db)["require_purpose"] and not purpose:
        raise NoticeError("Enter the purpose this footage is kept for, for example site security")
    return purpose, legal_basis


def _policy_rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM retention_policy_overrides").fetchall()


def policies(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every retention policy with its purpose: the site default first, then group and camera overrides. ``needs_purpose`` marks the ones to fill in."""
    config = settings(db)
    needs = lambda purpose: config["require_purpose"] and not purpose.strip()
    result: list[dict[str, Any]] = [{"scope": "default", "id": None, "name": "Site default", "retention_days": DEFAULT_RETENTION_DAYS, "purpose": config["default_purpose"],
                                     "legal_basis": config["default_legal_basis"], "needs_purpose": needs(config["default_purpose"])}]
    names = {("group", row["id"]): row["name"] for row in db.execute("SELECT id,name FROM camera_groups")}
    names.update({("camera", row["id"]): row["name"] for row in db.execute("SELECT id,name FROM cameras")})
    for row in sorted(_policy_rows(db), key=lambda item: (item["scope_type"] != "group", str(names.get((item["scope_type"], item["scope_id"]), "")).lower())):
        name = names.get((row["scope_type"], row["scope_id"]))
        if name is None:
            continue
        result.append({"scope": row["scope_type"], "id": row["scope_id"], "name": name, "retention_days": int(row["retention_days"]), "purpose": row["purpose"],
                       "legal_basis": row["legal_basis"], "needs_purpose": needs(row["purpose"])})
    return result


def deletions(db: sqlite3.Connection, *, camera_id: str | None = None, start: str | None = None, end: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
    """What automatic retention removed, newest day first (per camera and day)."""
    clauses, values = [], []
    if camera_id:
        clauses.append("camera_id=?")
        values.append(camera_id)
    if start:
        clauses.append("day>=?")
        values.append(start[:10])
    if end:
        clauses.append("day<=?")
        values.append(end[:10])
    rows = db.execute(f"SELECT * FROM retention_deletions {'WHERE ' + ' AND '.join(clauses) if clauses else ''} ORDER BY day DESC,camera_name COLLATE NOCASE LIMIT ?", (*values, limit)).fetchall()
    return [{"camera_id": row["camera_id"], "camera_name": row["camera_name"], "day": row["day"], "segments": row["segments"], "bytes": row["bytes"], "retention_days": row["retention_days"],
             "purpose": row["purpose"], "legal_basis": row["legal_basis"], "oldest_deleted_at": row["oldest_deleted_at"], "newest_deleted_at": row["newest_deleted_at"]} for row in rows]


# --- what the configuration says ---------------------------------------------------------------------------------------------------------------

def _cameras(db: sqlite3.Connection) -> list[dict[str, Any]]:
    cameras = []
    for row in db.execute("SELECT c.id,c.name,c.audio_enabled,c.recording_mode,c.recording_enabled,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id "
                          "WHERE c.archived_at IS NULL ORDER BY c.name COLLATE NOCASE,c.id"):
        days, source = effective_retention(db, row["id"])
        purpose, basis = effective_purpose(db, row["id"])
        cameras.append({"name": row["name"], "group": row["group_name"], "retention_days": days, "policy_source": source, "purpose": purpose, "legal_basis": basis,
                        "audio_recorded": bool(row["audio_enabled"]), "recording_mode": row["recording_mode"] if row["recording_enabled"] else "disabled"})
    return cameras


def notice(db: sqlite3.Connection) -> dict[str, Any]:
    """The camera notice (signage text) for the site as configured now."""
    config, cameras = settings(db), _cameras(db)
    purposes = list(dict.fromkeys(camera["purpose"] for camera in cameras if camera["purpose"]))
    recording = [camera for camera in cameras if camera["recording_mode"] != "disabled"]
    longest = max((camera["retention_days"] for camera in recording), default=None)
    audio = [camera["name"] for camera in recording if camera["audio_recorded"]]
    contact = ", ".join(item for item in (config["contact_name"], config["contact_email"], config["contact_phone"]) if item)
    lines = [f"Video surveillance (CCTV) is in operation{' at ' + config['organisation'] if config['organisation'] else ''}.",
             f"Purpose: {'; '.join(purposes) if purposes else 'not yet recorded'}.",
             f"Recordings are kept for up to {longest} days, then deleted automatically." if longest is not None else "No camera is recording.",
             f"Cameras: {', '.join(camera['name'] for camera in cameras)}." if cameras else "No cameras are set up.",
             f"Sound is also recorded by: {', '.join(audio)}." if audio else "Sound is not recorded.",
             f"Questions or requests about your data: {contact}." if contact else "Contact details have not been entered."]
    return {"title": "CCTV in operation", "organisation": config["organisation"], "purposes": purposes, "retention_days": longest, "audio_cameras": audio, "contact": contact,
            "lines": lines, "camera_count": len(cameras), "cameras": [camera["name"] for camera in cameras], "generated_at": _now().isoformat()}


def summary(db: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """The data-protection summary: cameras with their purposes, retention and audio, who can see them, what retention has deleted, and who to contact."""
    now = now or _now()
    config, cameras = settings(db), _cameras(db)
    everyone = len(cameras)
    people = []
    ids = [row["id"] for row in db.execute("SELECT id FROM cameras WHERE archived_at IS NULL")]
    for user in db.execute("SELECT id,username,role FROM users ORDER BY username COLLATE NOCASE"):
        seen = access.resolve(db, user["id"], user["role"], now)
        count = everyone if seen.unrestricted else len(seen.only(ids))
        people.append({"username": user["username"], "role": user["role"], "cameras": count, "all_cameras": seen.unrestricted or count == everyone})
    since = (now - timedelta(days=30)).date().isoformat()
    removed = db.execute("SELECT COALESCE(SUM(segments),0) AS segments,COALESCE(SUM(bytes),0) AS bytes FROM retention_deletions WHERE day>=?", (since,)).fetchone()
    return {"organisation": config["organisation"], "contact": {"name": config["contact_name"], "email": config["contact_email"], "phone": config["contact_phone"]},
            "require_purpose": config["require_purpose"], "cameras": cameras, "people": people, "audio_cameras": [camera["name"] for camera in cameras if camera["audio_recorded"] and camera["recording_mode"] != "disabled"],
            "missing_purpose": [camera["name"] for camera in cameras if not camera["purpose"]],
            "deleted_last_30_days": {"segments": removed["segments"], "bytes": removed["bytes"], "since": since},
            "generated_at": now.isoformat()}


# --- printable pages ---------------------------------------------------------------------------------------------------------------------------

_STYLE = ("body{font:16px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:46rem;padding:0 1rem;color:#111}h1{font-size:1.6rem}table{border-collapse:collapse;width:100%}"
          "th,td{border:1px solid #888;padding:.35rem .5rem;text-align:left;vertical-align:top}.sign{border:4px solid #111;padding:1.5rem;font-size:1.25rem}"
          ".sign h1{text-transform:uppercase;text-align:center}.muted{color:#555;font-size:.85rem}@media print{body{margin:0}button{display:none}}")


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{escape(title)}</title>"
            f"<style>{_STYLE}</style></head><body>{body}<p><button onclick=\"window.print()\">Print</button></p></body></html>")


def render_notice(data: dict[str, Any]) -> str:
    paragraphs = "".join(f"<p>{escape(line)}</p>" for line in data["lines"])
    return _page(data["title"], f"<div class=\"sign\"><h1>{escape(data['title'])}</h1>{paragraphs}</div><p class=\"muted\">Generated {escape(data['generated_at'][:16].replace('T', ' '))} UTC from the current settings.</p>")


def render_summary(data: dict[str, Any]) -> str:
    yes = lambda flag: "Yes" if flag else "No"
    rows = "".join(f"<tr><td>{escape(c['name'])}</td><td>{escape(c['group'] or '')}</td><td>{escape(c['purpose'] or 'Not recorded')}</td><td>{escape(c['legal_basis'] or '')}</td>"
                   f"<td>{c['retention_days']} days</td><td>{escape(c['recording_mode'])}</td><td>{yes(c['audio_recorded'])}</td></tr>" for c in data["cameras"])
    people = "".join(f"<tr><td>{escape(p['username'])}</td><td>{escape(p['role'])}</td><td>{'All cameras' if p['all_cameras'] else str(p['cameras']) + ' cameras'}</td></tr>" for p in data["people"])
    contact = ", ".join(escape(item) for item in data["contact"].values() if item) or "Not entered"
    removed = data["deleted_last_30_days"]
    body = (f"<h1>Data-protection summary</h1><p>{escape(data['organisation'])}</p>"
            f"<h2>Cameras, purposes and retention</h2><table><tr><th>Camera</th><th>Group</th><th>Purpose</th><th>Legal basis</th><th>Kept</th><th>Recording</th><th>Audio</th></tr>{rows}</table>"
            f"<h2>Who can view the cameras</h2><table><tr><th>Person</th><th>Role</th><th>Access</th></tr>{people}</table>"
            f"<h2>Automatic deletion</h2><p>Since {escape(removed['since'])}: {removed['segments']} recordings ({removed['bytes']} bytes) deleted when their retention period ended.</p>"
            f"<h2>Contact</h2><p>{contact}</p><p class=\"muted\">Generated {escape(data['generated_at'][:16].replace('T', ' '))} UTC from the current settings.</p>")
    return _page("Data-protection summary", body)
