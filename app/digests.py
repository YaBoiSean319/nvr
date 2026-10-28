"""Event digests (#136): a written account of a time range for the site, groups or cameras, made only from stored data.

A digest lists what happened in the range, each item with a link to its clip or record: the alarms (by rule and by outcome), notable tracks
(people after hours, vehicles at gates), the gaps in recording and why, and system incidents; and how much of the time the AI analysed, so a
quiet digest in time nobody looked at is not read as a quiet time. It is built the same way every time from the same data and needs no language
model. An administrator may configure a local, OpenAI-compatible model endpoint to add one summarising paragraph; it is off until then, and while
it is off no request leaves the machine.

A digest is made for one person's view: it never includes a camera that person may not see, and the system-wide incidents (those that name no
camera) only if they may see every camera.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import access, coverage, rule_actions, search
from .credentials import decrypt, encrypt
from .store import connect
from .webhooks import validate_target_url

logger = logging.getLogger("nvr.digests")

ALARM_ITEMS = 200
TRACK_ITEMS = 50
GAP_ITEMS = 20
INCIDENT_ITEMS = 50
MAX_RANGE = timedelta(days=31)
MAX_SCHEDULES = 50
KEEP_DIGESTS = timedelta(days=90)
DEFAULT_AFTER_HOURS = {"start": "22:00", "end": "06:00"}
SUMMARY_TIMEOUT_SECONDS = 60
SUMMARY_MAX_CHARS = 1500
SUMMARY_NOTE = "Written by the local model the administrator configured, from the lists below. The lists are the record."
NOTABLE_RULE_TYPES = ("zone_entry", "line_crossing")
MAX_ATTEMPTS = rule_actions.MAX_ATTEMPTS


class DigestError(ValueError):
    """A schedule or digest request cannot be used as given."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def site_zone(db: sqlite3.Connection) -> ZoneInfo:
    row = db.execute("SELECT time_zone FROM site_display_settings WHERE singleton_id=1").fetchone()
    try:
        return ZoneInfo(row["time_zone"] if row else "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _clock(value: str) -> tuple[int, int]:
    try:
        hours, minutes = (int(part) for part in value.split(":"))
    except ValueError:
        raise DigestError("Times are written HH:MM") from None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise DigestError("Times are written HH:MM")
    return hours, minutes


def _links(public: str, kind: str, **values: str) -> str:
    base = public.rstrip("/")
    if kind == "alarm":
        return f"{base}/?alarm={quote(values['id'])}"
    if kind == "incident":
        return f"{base}/?incident={quote(values['id'])}"
    return f"{base}/playback?playback={quote(values['camera_id'])}&at={quote(values['at'])}"


# --- scope and shifts ------------------------------------------------------------------------------------------------------------------------

def scope_cameras(db: sqlite3.Connection, scope: dict[str, Any]) -> list[str]:
    """The cameras a scope names ({"site": true}, or group_ids and/or camera_ids), live cameras only, in name order."""
    rows = db.execute("SELECT id,group_id FROM cameras WHERE archived_at IS NULL ORDER BY name COLLATE NOCASE,id").fetchall()
    if scope.get("site"):
        return [row["id"] for row in rows]
    groups = set(scope.get("group_ids") or [])
    members = {row[0] for row in db.execute(f"SELECT camera_id FROM camera_group_memberships WHERE group_id IN ({','.join('?' * len(groups)) or 'NULL'})", tuple(groups))}
    chosen = set(scope.get("camera_ids") or [])
    return [row["id"] for row in rows if row["id"] in chosen or row["group_id"] in groups or row["id"] in members]


def validate_scope(db: sqlite3.Connection, scope: dict[str, Any]) -> dict[str, Any]:
    if scope.get("site"):
        return {"site": True}
    groups, cameras = list(dict.fromkeys(scope.get("group_ids") or [])), list(dict.fromkeys(scope.get("camera_ids") or []))
    if not groups and not cameras:
        raise DigestError("Choose the site, a group or a camera")
    known_groups = {row[0] for row in db.execute("SELECT id FROM camera_groups")}
    known_cameras = {row[0] for row in db.execute("SELECT id FROM cameras WHERE archived_at IS NULL")}
    if not set(groups) <= known_groups or not set(cameras) <= known_cameras:
        raise DigestError("A group or camera in the scope does not exist")
    return {"site": False, "group_ids": groups, "camera_ids": cameras}


def validate_shift(shift: dict[str, Any]) -> dict[str, Any]:
    """{"days": weekdays the shift ends on (0 Monday to 6 Sunday), "start": "HH:MM", "end": "HH:MM"} in the site's time zone."""
    days = sorted({int(day) for day in shift.get("days") or []})
    if not days or any(day not in range(7) for day in days):
        raise DigestError("Choose the days a shift ends on")
    _clock(shift["start"]), _clock(shift["end"])
    return {"days": days, "start": shift["start"], "end": shift["end"]}


def latest_shift(shift: dict[str, Any], zone: ZoneInfo, now: datetime) -> tuple[datetime, datetime]:
    """The most recent shift that has ended by ``now``: (start, end) in UTC. A shift whose end is not after its start began the day before."""
    end_hour, end_minute = _clock(shift["end"])
    start_hour, start_minute = _clock(shift["start"])
    length = (timedelta(hours=end_hour, minutes=end_minute) - timedelta(hours=start_hour, minutes=start_minute)) % timedelta(days=1) or timedelta(days=1)
    local_now = now.astimezone(zone)
    for back in range(0, 9):
        day = (local_now - timedelta(days=back)).date()
        if day.weekday() not in shift["days"]:
            continue
        end = datetime(day.year, day.month, day.day, end_hour, end_minute, tzinfo=zone).astimezone(timezone.utc)
        if end <= now:
            return end - length, end
    raise DigestError("The shift never ends")


# --- building a digest -----------------------------------------------------------------------------------------------------------------------

def _after_hours(moment: datetime, window: dict[str, str], zone: ZoneInfo) -> bool:
    local = moment.astimezone(zone)
    start, end = _clock(window["start"]), _clock(window["end"])
    now_minutes, start_minutes, end_minutes = local.hour * 60 + local.minute, start[0] * 60 + start[1], end[0] * 60 + end[1]
    if start_minutes == end_minutes:
        return False
    return start_minutes <= now_minutes or now_minutes < end_minutes if start_minutes > end_minutes else start_minutes <= now_minutes < end_minutes


def build(db: sqlite3.Connection, camera_ids: list[str], start: datetime, end: datetime, *, unrestricted: bool, public_url: str = "",
          after_hours: dict[str, str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The digest of [start, end) for these cameras. ``unrestricted`` is whether the reader may see every camera (system-wide incidents are theirs only then)."""
    now = now or _now()
    window = after_hours or DEFAULT_AFTER_HOURS
    zone = site_zone(db)
    names = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM cameras")}
    camera_ids = [camera_id for camera_id in camera_ids if camera_id in names]
    marks = ",".join("?" * len(camera_ids)) or "NULL"
    start_text, end_text = _iso(start), _iso(end)

    alarm_rows = db.execute(f"SELECT id,rule_id,rule_name,camera_id,label,state,occurred_at FROM alarms WHERE camera_id IN ({marks}) AND occurred_at>=? AND occurred_at<? ORDER BY occurred_at,id",
                            (*camera_ids, start_text, end_text)).fetchall()
    by_rule: dict[tuple[str | None, str], dict[str, Any]] = {}
    for row in alarm_rows:
        entry = by_rule.setdefault((row["rule_id"], row["rule_name"]), {"rule_id": row["rule_id"], "rule_name": row["rule_name"], "total": 0,
                                                                         "outcomes": {"new": 0, "acknowledged": 0, "assigned": 0, "resolved": 0}})
        entry["total"] += 1
        entry["outcomes"][row["state"]] += 1
    alarms = {
        "total": len(alarm_rows), "unresolved": sum(1 for row in alarm_rows if row["state"] != "resolved"),
        "by_rule": sorted(by_rule.values(), key=lambda item: (-item["total"], item["rule_name"].lower())),
        "items": [{"id": row["id"], "rule_name": row["rule_name"], "camera_id": row["camera_id"], "camera_name": names[row["camera_id"]], "label": row["label"],
                   "state": row["state"], "occurred_at": row["occurred_at"], "link": _links(public_url, "alarm", id=row["id"])} for row in alarm_rows[:ALARM_ITEMS]],
        "truncated": len(alarm_rows) > ALARM_ITEMS,
    }

    people = [row for row in db.execute(f"SELECT id,camera_id,first_seen_at FROM ai_tracks WHERE camera_id IN ({marks}) AND label='person' AND first_seen_at>=? AND first_seen_at<? "
                                        "ORDER BY first_seen_at,id", (*camera_ids, start_text, end_text)) if _after_hours(_parse(row["first_seen_at"]), window, zone)]
    gates = db.execute(f"SELECT e.id,e.camera_id,e.rule_name,e.occurred_at,e.condition_type,a.id AS alarm_id FROM rule_events e LEFT JOIN alarms a ON a.rule_event_id=e.id "
                       f"WHERE e.camera_id IN ({marks}) AND e.label='vehicle' AND e.condition_type IN ({','.join('?' * len(NOTABLE_RULE_TYPES))}) AND e.occurred_at>=? AND e.occurred_at<? "
                       "ORDER BY e.occurred_at,e.id", (*camera_ids, *NOTABLE_RULE_TYPES, start_text, end_text)).fetchall()
    tracks = {
        "after_hours_window": window,
        "after_hours_people": {"total": len(people), "items": [{"track_id": row["id"], "camera_id": row["camera_id"], "camera_name": names[row["camera_id"]],
                                                                 "first_seen_at": row["first_seen_at"],
                                                                 "link": _links(public_url, "playback", camera_id=row["camera_id"], at=row["first_seen_at"])} for row in people[:TRACK_ITEMS]]},
        "vehicles_at_gates": {"total": len(gates), "items": [{"event_id": row["id"], "rule_name": row["rule_name"], "camera_id": row["camera_id"], "camera_name": names[row["camera_id"]],
                                                              "occurred_at": row["occurred_at"],
                                                              "link": _links(public_url, "alarm", id=row["alarm_id"]) if row["alarm_id"]
                                                              else _links(public_url, "playback", camera_id=row["camera_id"], at=row["occurred_at"])} for row in gates[:TRACK_ITEMS]]},
    }

    gap_items, gap_total, by_camera = [], 0.0, []
    for camera_id in camera_ids:
        since = max(start, _parse(db.execute("SELECT created_at FROM cameras WHERE id=?", (camera_id,)).fetchone()[0]))
        until = min(end, now)
        if since >= until:
            continue
        found = [gap for gap in coverage.timeline(db, camera_id, since, until, limit=None)["gaps"] if gap["reason_code"] not in coverage.EXPECTED]
        seconds = sum((_parse(gap["ends_at"]) - _parse(gap["starts_at"])).total_seconds() for gap in found)
        if not found:
            continue
        gap_total += seconds
        by_camera.append({"camera_id": camera_id, "camera_name": names[camera_id], "gaps": len(found), "seconds": round(seconds, 1)})
        for gap in sorted(found, key=lambda item: item["starts_at"])[:GAP_ITEMS]:
            gap_items.append({"camera_id": camera_id, "camera_name": names[camera_id], "starts_at": gap["starts_at"], "ends_at": gap["ends_at"], "state": gap["state"],
                              "reason": gap["reason"], "reason_code": gap["reason_code"],
                              "seconds": round((_parse(gap["ends_at"]) - _parse(gap["starts_at"])).total_seconds(), 1),
                              "link": _links(public_url, "playback", camera_id=camera_id, at=gap["starts_at"])})
    gaps = {"total_seconds": round(gap_total, 1), "cameras": by_camera, "items": gap_items}

    incident_rows = [row for row in db.execute("SELECT * FROM incidents WHERE first_seen_at<? AND last_seen_at>=? ORDER BY first_seen_at,id", (end_text, start_text))
                     if (row["camera_id"] in names and row["camera_id"] in camera_ids) or (row["camera_id"] is None and unrestricted)]
    incidents = {"total": len(incident_rows), "items": [{
        "id": row["id"], "camera_id": row["camera_id"], "camera_name": names.get(row["camera_id"]) if row["camera_id"] else None, "fault_type": row["fault_type"],
        "summary": row["summary"], "state": row["state"], "first_seen_at": row["first_seen_at"], "last_seen_at": row["last_seen_at"], "recovered_at": row["recovered_at"],
        "link": _links(public_url, "incident", id=row["id"])} for row in incident_rows[:INCIDENT_ITEMS]]}

    report = search.coverage(db, {camera_id: names[camera_id] for camera_id in camera_ids}, start, min(end, now)) if camera_ids else None
    analysis = {"analysed_percent": report["analysed_percent"] if report else None, "fully_analysed": report["fully_analysed"] if report else True,
                "cameras": [{"camera_id": item["camera_id"], "camera_name": item["camera_name"], "unanalysed_seconds": item["unanalysed_seconds"]} for item in (report["cameras"] if report else [])]}
    return {"period": {"start": start_text, "end": end_text}, "time_zone": zone.key, "cameras": [{"id": camera_id, "name": names[camera_id]} for camera_id in camera_ids],
            "alarms": alarms, "tracks": tracks, "gaps": gaps, "incidents": incidents, "analysis": analysis}


# --- reading it ------------------------------------------------------------------------------------------------------------------------------

def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def _when(iso: str, zone: ZoneInfo) -> str:
    return _parse(iso).astimezone(zone).strftime("%a %d %b %H:%M")


def headline(body: dict[str, Any]) -> str:
    alarms, tracks, gaps, incidents = body["alarms"], body["tracks"], body["gaps"], body["incidents"]
    parts = [f"{alarms['total']} alarm{'' if alarms['total'] == 1 else 's'} ({alarms['unresolved']} not resolved)",
             f"{tracks['after_hours_people']['total']} after-hours {'person' if tracks['after_hours_people']['total'] == 1 else 'people'}",
             f"{tracks['vehicles_at_gates']['total']} vehicle{'' if tracks['vehicles_at_gates']['total'] == 1 else 's'} at gates",
             f"{_duration(gaps['total_seconds'])} not recorded" if gaps["total_seconds"] else "no recording gaps",
             f"{incidents['total']} system incident{'' if incidents['total'] == 1 else 's'}"]
    return ", ".join(parts)


def render_text(name: str, body: dict[str, Any], summary: str | None = None) -> str:
    """The digest as plain text with its links: the same words every time for the same data."""
    zone = ZoneInfo(body["time_zone"])
    lines = [f"{name}", f"{_when(body['period']['start'], zone)} to {_when(body['period']['end'], zone)} ({zone.key})",
             f"Cameras: {', '.join(camera['name'] for camera in body['cameras']) or 'none'}", "", headline(body)]
    if summary:
        lines += ["", f"Summary ({SUMMARY_NOTE})", summary]
    analysis = body["analysis"]
    if not analysis["fully_analysed"]:
        lines += ["", f"Not all of this time was analysed ({analysis['analysed_percent']}% was): quiet stretches may mean nothing was looked at."]
        lines += [f"  {item['camera_name']}: {_duration(item['unanalysed_seconds'])} not analysed" for item in analysis["cameras"]]
    alarms = body["alarms"]
    lines += ["", "ALARMS"]
    lines += [f"  {item['rule_name']}: {item['total']} ({', '.join(f'{count} {state}' for state, count in item['outcomes'].items() if count)})" for item in alarms["by_rule"]] or ["  None"]
    lines += [f"  - {_when(item['occurred_at'], zone)} {item['rule_name']} on {item['camera_name']} [{item['state']}] {item['link']}" for item in alarms["items"]]
    if alarms["truncated"]:
        lines.append(f"  ... and {alarms['total'] - len(alarms['items'])} more")
    tracks = body["tracks"]
    window = tracks["after_hours_window"]
    lines += ["", f"PEOPLE AFTER HOURS ({window['start']} to {window['end']})"]
    lines += [f"  - {_when(item['first_seen_at'], zone)} on {item['camera_name']} {item['link']}" for item in tracks["after_hours_people"]["items"]] or ["  None"]
    lines += ["", "VEHICLES AT GATES"]
    lines += [f"  - {_when(item['occurred_at'], zone)} {item['rule_name']} on {item['camera_name']} {item['link']}" for item in tracks["vehicles_at_gates"]["items"]] or ["  None"]
    gaps = body["gaps"]
    lines += ["", "RECORDING GAPS"]
    lines += [f"  - {item['camera_name']} {_when(item['starts_at'], zone)} for {_duration(item['seconds'])}: {item['reason']} {item['link']}" for item in gaps["items"]] or ["  None"]
    lines += ["", "SYSTEM INCIDENTS"]
    lines += [f"  - {_when(item['first_seen_at'], zone)} {item['summary']}{' on ' + item['camera_name'] if item['camera_name'] else ''} [{item['state']}] {item['link']}" for item in body["incidents"]["items"]] or ["  None"]
    return "\n".join(lines) + "\n"


def restrict(body: dict[str, Any], visible: set[str] | None, unrestricted: bool) -> dict[str, Any]:
    """The digest as this reader may see it: items on cameras they may not see are removed, and so are the totals that counted them.

    ``visible`` is None for a reader who may see everything. A stored digest is restricted again when read, so access taken away since it was made is respected."""
    if visible is None:
        return body
    def keep(item: dict[str, Any]) -> bool:
        return item.get("camera_id") in visible or (item.get("camera_id") is None and unrestricted)
    result = json.loads(json.dumps(body))
    result["cameras"] = [camera for camera in result["cameras"] if camera["id"] in visible]
    alarms = result["alarms"]
    alarms["items"] = [item for item in alarms["items"] if keep(item)]
    # Per-rule totals cannot be recomputed from the (possibly truncated) items, so a restricted reader gets totals of what they can see.
    by_rule: dict[tuple[str | None, str], dict[str, Any]] = {}
    for item in alarms["items"]:
        entry = by_rule.setdefault((item.get("rule_id"), item["rule_name"]), {"rule_id": item.get("rule_id"), "rule_name": item["rule_name"], "total": 0,
                                                                              "outcomes": {"new": 0, "acknowledged": 0, "assigned": 0, "resolved": 0}})
        entry["total"] += 1
        entry["outcomes"][item["state"]] += 1
    alarms["by_rule"] = sorted(by_rule.values(), key=lambda entry: (-entry["total"], entry["rule_name"].lower()))
    alarms["total"], alarms["unresolved"] = len(alarms["items"]), sum(1 for item in alarms["items"] if item["state"] != "resolved")
    tracks = result["tracks"]
    for key in ("after_hours_people", "vehicles_at_gates"):
        tracks[key]["items"] = [item for item in tracks[key]["items"] if keep(item)]
        tracks[key]["total"] = len(tracks[key]["items"])
    gaps = result["gaps"]
    gaps["items"] = [item for item in gaps["items"] if keep(item)]
    gaps["cameras"] = [item for item in gaps["cameras"] if keep(item)]
    gaps["total_seconds"] = round(sum(item["seconds"] for item in gaps["cameras"]), 1)
    result["incidents"]["items"] = [item for item in result["incidents"]["items"] if keep(item)]
    result["incidents"]["total"] = len(result["incidents"]["items"])
    result["analysis"]["cameras"] = [item for item in result["analysis"]["cameras"] if keep(item)]
    return result


# --- schedules -------------------------------------------------------------------------------------------------------------------------------

def _settings(action: dict[str, Any]) -> dict[str, Any]:
    return rule_actions._settings(action)


def save_actions(db: sqlite3.Connection, schedule_id: str, actions: list[dict[str, Any]]) -> None:
    """Replace a schedule's actions inside the caller's transaction (webhook, Slack, Teams or MQTT; saved secrets are kept unless new ones are sent)."""
    if len(actions) > rule_actions.MAX_ACTIONS:
        raise DigestError(f"A schedule delivers through at most {rule_actions.MAX_ACTIONS} actions")
    existing = {row["id"]: row for row in db.execute("SELECT * FROM digest_actions WHERE schedule_id=?", (schedule_id,))}
    kept: set[str] = set()
    for position, action in enumerate(actions):
        kind = action["type"]
        if kind not in ("webhook", "mqtt", "slack", "teams"):
            raise DigestError(f"{action['name']}: a digest cannot be delivered by a {kind} action")
        previous = existing.get(action.get("id") or "")
        current = json.loads(decrypt(previous["secret_ciphertext"])) if previous is not None and previous["secret_ciphertext"] and previous["type"] == kind else None
        try:
            settings = _settings(action)
            secret, target = rule_actions._secret(kind, action, current, settings)
        except ValueError as exc:
            raise DigestError(str(exc)) from exc
        values = (position, kind, action["name"], int(action.get("enabled", True)), json.dumps(settings, separators=(",", ":")), encrypt(json.dumps(secret)) if secret else None, target, _iso(_now()))
        if previous is not None:
            db.execute("UPDATE digest_actions SET position=?,type=?,name=?,enabled=?,settings_json=?,secret_ciphertext=?,target=?,updated_at=? WHERE id=?", (*values, previous["id"]))
            kept.add(previous["id"])
        else:
            db.execute("INSERT INTO digest_actions(position,type,name,enabled,settings_json,secret_ciphertext,target,updated_at,id,schedule_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (*values, str(uuid.uuid4()), schedule_id))
    for action_id in set(existing) - kept:
        db.execute("DELETE FROM digest_actions WHERE id=?", (action_id,))


def action_views(db: sqlite3.Connection, schedule_id: str) -> list[dict[str, Any]]:
    return [rule_actions._view(row) for row in db.execute("SELECT * FROM digest_actions WHERE schedule_id=? ORDER BY position", (schedule_id,))]


def schedule_view(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    recipient = db.execute("SELECT username FROM users WHERE id=?", (row["recipient_user_id"],)).fetchone()
    zone = site_zone(db)
    shift = json.loads(row["shift_json"])
    try:
        _, upcoming = latest_shift(shift, zone, _now())
    except DigestError:
        upcoming = None
    return {"id": row["id"], "name": row["name"], "enabled": bool(row["enabled"]), "scope": json.loads(row["scope_json"]), "recipient_user_id": row["recipient_user_id"],
            "recipient": recipient["username"] if recipient else None, "shift": shift, "after_hours": json.loads(row["after_hours_json"]), "summarise": bool(row["summarise"]),
            "last_period_end": row["last_period_end"], "last_shift_end": _iso(upcoming) if upcoming else None, "time_zone": zone.key, "actions": action_views(db, row["id"])}


def _reader(db: sqlite3.Connection, user_id: int) -> tuple[set[str] | None, bool]:
    user = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    if user is None:
        return set(), False
    view = access.resolve(db, user_id, user["role"])
    return (None if view.unrestricted else set(view.cameras or ())), view.unrestricted


def _public_url() -> str:
    return rule_actions._public_url()


def generate(db: sqlite3.Connection, schedule: sqlite3.Row, start: datetime, end: datetime, *, now: datetime | None = None) -> str | None:
    """Make, keep and queue the digest of a schedule for [start, end); None if that period already has one. Inside the caller's transaction."""
    if db.execute("SELECT 1 FROM digests WHERE schedule_id=? AND period_end=?", (schedule["id"], _iso(end))).fetchone():
        return None
    visible, unrestricted = _reader(db, schedule["recipient_user_id"])
    cameras = scope_cameras(db, json.loads(schedule["scope_json"]))
    if visible is not None:
        cameras = [camera_id for camera_id in cameras if camera_id in visible]
    public = _public_url()
    body = build(db, cameras, start, end, unrestricted=unrestricted, public_url=public, after_hours=json.loads(schedule["after_hours_json"]), now=now)
    digest_id = str(uuid.uuid4())
    summary = summary_error = None
    text = render_text(schedule["name"], body)
    if schedule["summarise"]:
        summary, summary_error = summarise_now(db, text)
        if summary:
            text = render_text(schedule["name"], body, summary)
    db.execute("INSERT INTO digests(id,schedule_id,name,recipient_user_id,period_start,period_end,body_json,text,summary,summary_error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (digest_id, schedule["id"], schedule["name"], schedule["recipient_user_id"], _iso(start), _iso(end), json.dumps(body, separators=(",", ":")), text, summary, summary_error,
                _iso(now or _now())))
    queue_deliveries(db, digest_id, schedule["id"], schedule["name"], body, text)
    return digest_id


def queue_deliveries(db: sqlite3.Connection, digest_id: str, schedule_id: str, name: str, body: dict[str, Any], text: str) -> int:
    public = _public_url()
    payload = json.dumps({
        "event_id": digest_id, "event_type": "digest.ready", "digest_id": digest_id, "name": name, "period": body["period"], "headline": headline(body),
        "alarms": body["alarms"]["total"], "after_hours_people": body["tracks"]["after_hours_people"]["total"], "vehicles_at_gates": body["tracks"]["vehicles_at_gates"]["total"],
        "gap_seconds": body["gaps"]["total_seconds"], "incidents": body["incidents"]["total"], "fully_analysed": body["analysis"]["fully_analysed"],
        "digest_url": f"{public}/digests/{digest_id}" if public else None, "text": text,
        "message": f"{name}: {headline(body)}",
    }, sort_keys=True, separators=(",", ":"))
    now = _iso(_now())
    return sum(db.execute(
        "INSERT OR IGNORE INTO digest_deliveries(id,digest_id,action_id,action_name,action_type,payload_json,next_attempt_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), digest_id, action["id"], action["name"], action["type"], payload, now, now, now)).rowcount
        for action in db.execute("SELECT id,name,type FROM digest_actions WHERE schedule_id=? AND enabled=1 ORDER BY position", (schedule_id,)).fetchall())


def last_finished_shift_end(db: sqlite3.Connection, shift: dict[str, Any], now: datetime | None = None) -> str:
    """Where a new schedule begins: the end of the shift that last finished, so its first digest is the next one."""
    return _iso(latest_shift(shift, site_zone(db), now or _now())[1])


def generate_due(now: datetime | None = None) -> list[str]:
    """Make the digest of the latest finished shift for each enabled schedule that has not made it yet (a long outage skips shifts rather than flooding)."""
    now = now or _now()
    made: list[str] = []
    with connect() as db:
        schedules = db.execute("SELECT * FROM digest_schedules WHERE enabled=1 ORDER BY created_at,id").fetchall()
        zone = site_zone(db)
    for schedule in schedules:
        try:
            start, end = latest_shift(json.loads(schedule["shift_json"]), zone, now)
        except DigestError:
            continue
        if _iso(end) <= schedule["last_period_end"]:
            continue
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                fresh = db.execute("SELECT * FROM digest_schedules WHERE id=? AND enabled=1", (schedule["id"],)).fetchone()
                if fresh is None or _iso(end) <= fresh["last_period_end"]:
                    db.execute("ROLLBACK")
                    continue
                digest_id = generate(db, fresh, start, end, now=now)
                db.execute("UPDATE digest_schedules SET last_period_end=? WHERE id=?", (_iso(end), schedule["id"]))
                db.execute("DELETE FROM digests WHERE created_at<?", (_iso(now - KEEP_DIGESTS),))
            except BaseException:
                db.execute("ROLLBACK")
                logger.exception("The digest of schedule %s could not be made", schedule["id"])
                continue
            db.execute("COMMIT")
        if digest_id:
            made.append(digest_id)
    return made


# --- the optional summarising model ----------------------------------------------------------------------------------------------------------

def model_settings(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute("SELECT * FROM digest_settings WHERE singleton_id=1").fetchone()
    return {"summary_enabled": bool(row["summary_enabled"]), "configured": row["summary_url_ciphertext"] is not None, "summary_model": row["summary_model"],
            "allow_private": bool(row["summary_allow_private"])}


def save_model_settings(db: sqlite3.Connection, *, enabled: bool, url: str | None, model: str, allow_private: bool) -> None:
    row = db.execute("SELECT summary_url_ciphertext FROM digest_settings WHERE singleton_id=1").fetchone()
    ciphertext = row["summary_url_ciphertext"]
    if url:
        try:
            validate_target_url(url, allow_private)
        except ValueError as exc:
            raise DigestError(str(exc)) from exc
        ciphertext = encrypt(url.strip())
    if enabled and (ciphertext is None or not model.strip()):
        raise DigestError("Enter the model's address and name before turning summaries on")
    db.execute("UPDATE digest_settings SET summary_enabled=?,summary_url_ciphertext=?,summary_model=?,summary_allow_private=?,updated_at=? WHERE singleton_id=1",
               (int(enabled), ciphertext, model.strip(), int(allow_private), _iso(_now())))


def summarise_now(db: sqlite3.Connection, text: str) -> tuple[str | None, str | None]:
    """(summary, error). With no endpoint configured and enabled, nothing is sent anywhere and both are None."""
    row = db.execute("SELECT * FROM digest_settings WHERE singleton_id=1").fetchone()
    if not row["summary_enabled"] or row["summary_url_ciphertext"] is None:
        return None, None
    url, model, allow_private = decrypt(row["summary_url_ciphertext"]), row["summary_model"], bool(row["summary_allow_private"])
    try:
        target = validate_target_url(url, allow_private)
        response = httpx.post(target, json={"model": model, "temperature": 0, "messages": [
            {"role": "system", "content": "Summarise this security digest in one short paragraph for a shift supervisor. Use only the facts given; add nothing."},
            {"role": "user", "content": text}]}, timeout=httpx.Timeout(SUMMARY_TIMEOUT_SECONDS, connect=5), follow_redirects=False, trust_env=False)
        response.raise_for_status()
        content = str(response.json()["choices"][0]["message"]["content"]).strip()
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        return None, f"The summary could not be written ({type(exc).__name__})"
    return (content[:SUMMARY_MAX_CHARS] or None), (None if content else "The model returned no text")


# --- delivery --------------------------------------------------------------------------------------------------------------------------------

def recover_inflight() -> None:
    with connect() as db:
        db.execute("UPDATE digest_deliveries SET status='queued',next_attempt_at=?,last_error='process_restarted' WHERE status='sending'", (_iso(_now()),))


def _claim() -> dict[str, Any] | None:
    now = _iso(_now())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT d.*,a.settings_json,a.secret_ciphertext,a.enabled AS action_enabled FROM digest_deliveries d LEFT JOIN digest_actions a ON a.id=d.action_id "
                             "WHERE d.status='queued' AND d.next_attempt_at<=? ORDER BY d.next_attempt_at,d.created_at LIMIT 1", (now,)).fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            attempt = int(row["attempt_count"]) + 1
            db.execute("UPDATE digest_deliveries SET status='sending',attempt_count=?,updated_at=? WHERE id=?", (attempt, now, row["id"]))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {**dict(row), "attempt_count": attempt}


def _finish(delivery_id: str, attempt: int, *, response_status: int | None = None, error: str | None = None, retryable: bool = False) -> None:
    now = _now()
    with connect() as db:
        if error is None:
            db.execute("UPDATE digest_deliveries SET status='delivered',response_status=?,last_error=NULL,updated_at=?,delivered_at=? WHERE id=?",
                       (response_status, _iso(now), _iso(now), delivery_id))
        else:
            retry = retryable and attempt < MAX_ATTEMPTS
            next_at = now + timedelta(seconds=rule_actions.RETRY_SECONDS[min(attempt - 1, len(rule_actions.RETRY_SECONDS) - 1)])
            db.execute("UPDATE digest_deliveries SET status=?,response_status=?,last_error=?,next_attempt_at=?,updated_at=? WHERE id=?",
                       ("queued" if retry else "failed", response_status, error, _iso(next_at), _iso(now), delivery_id))


def deliveries(schedule_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT d.* FROM digest_deliveries d JOIN digests g ON g.id=d.digest_id WHERE g.schedule_id=? ORDER BY d.created_at DESC,d.id LIMIT ?",
                          (schedule_id, min(max(limit, 1), 500))).fetchall()
    return [{key: row[key] for key in ("id", "digest_id", "action_id", "action_name", "action_type", "status", "attempt_count", "next_attempt_at", "response_status", "last_error",
                                      "created_at", "updated_at", "delivered_at")} for row in rows]


async def deliver_one() -> bool:
    delivery = await asyncio.to_thread(_claim)
    if delivery is None:
        return False
    attempt = int(delivery["attempt_count"])
    if delivery["action_id"] is None or not delivery["action_enabled"]:
        await asyncio.to_thread(_finish, delivery["id"], attempt, error="action_removed_or_disabled")
        return True
    status, error, retryable = await rule_actions._perform(delivery)
    await asyncio.to_thread(_finish, delivery["id"], attempt, response_status=status, error=error, retryable=retryable)
    return True


async def deliver_due() -> int:
    delivered = 0
    while await deliver_one():
        delivered += 1
    return delivered


class DigestManager:
    """Makes the digest of each schedule when its shift ends, and delivers what was queued."""

    def __init__(self, poll_seconds: float = 20.0) -> None:
        self.poll_seconds = poll_seconds
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await asyncio.to_thread(recover_inflight)
        self.task = asyncio.create_task(self._run(), name="digests")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(generate_due)
                while await deliver_one():
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Digest pass failed")
            await asyncio.sleep(self.poll_seconds)
