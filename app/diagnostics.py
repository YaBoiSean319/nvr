"""Opt-in, bounded diagnostics containing only explicitly approved metadata."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .store import connect

MAX_EVENTS = 1000
RETENTION_DAYS = 7
EVENT_FIELDS = {
    "discovery_scan": {
        "outcome": {"completed", "failed"},
        "elapsed_ms": (0, 60000),
        "interface_configured": {True, False},
        "discovered_count": (0, 10000),
        "ignored_responses": (0, 10000),
        "error_category": {"network", "configuration"},
    },
    "stream_probe": {
        "outcome": {"success", "unavailable"},
        "elapsed_ms": (0, 60000),
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _capture_until(db) -> datetime | None:
    row = db.execute("SELECT value FROM diagnostic_settings WHERE key='capture_until'").fetchone()
    if not row:
        return None
    try:
        value = datetime.fromisoformat(row["value"])
        return value if value > _now() else None
    except ValueError:
        return None


def capture_status() -> dict[str, object]:
    with connect() as db:
        until = _capture_until(db)
        count = db.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0]
    return {"enabled": until is not None, "capture_until": until.isoformat() if until else None, "event_count": count,
            "max_events": MAX_EVENTS, "retention_days": RETENTION_DAYS}


def set_capture(enabled: bool, hours: int) -> dict[str, object]:
    with connect() as db:
        if enabled:
            until = (_now() + timedelta(hours=hours)).isoformat()
            db.execute("INSERT INTO diagnostic_settings(key,value) VALUES('capture_until',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (until,))
        else:
            db.execute("DELETE FROM diagnostic_settings WHERE key='capture_until'")
    return capture_status()


def _safe_fields(event_type: str, fields: dict[str, object]) -> dict[str, object]:
    schema = EVENT_FIELDS.get(event_type)
    if schema is None:
        return {}
    safe: dict[str, object] = {}
    for key, value in fields.items():
        rule = schema.get(key)
        if isinstance(rule, set):
            try:
                if value in rule:
                    safe[key] = value
            except TypeError:
                continue
        elif isinstance(rule, tuple) and type(value) is int and rule[0] <= value <= rule[1]:
            safe[key] = value
    return safe


def record_event(event_type: str, **fields: object) -> None:
    """Ignore unapproved fields and do no collection while the opt-in window is off."""
    safe = _safe_fields(event_type, fields)
    if not safe:
        return
    with connect() as db:
        if _capture_until(db) is None:
            return
        db.execute("INSERT INTO diagnostic_events(event_type,fields_json) VALUES(?,?)", (event_type, json.dumps(safe, separators=(",", ":"))))
        cutoff = (_now() - timedelta(days=RETENTION_DAYS)).isoformat()
        db.execute("DELETE FROM diagnostic_events WHERE created_at < ?", (cutoff,))
        db.execute("DELETE FROM diagnostic_events WHERE id NOT IN (SELECT id FROM diagnostic_events ORDER BY id DESC LIMIT ?)", (MAX_EVENTS,))


def bundle() -> dict[str, object]:
    with connect() as db:
        until = _capture_until(db)
        rows = db.execute("SELECT created_at,event_type,fields_json FROM diagnostic_events ORDER BY id DESC LIMIT ?", (MAX_EVENTS,)).fetchall()
    events = []
    for row in reversed(rows):
        if row["event_type"] not in EVENT_FIELDS:
            continue
        try:
            fields = _safe_fields(row["event_type"], json.loads(row["fields_json"]))
        except (TypeError, json.JSONDecodeError):
            continue
        events.append({"timestamp": row["created_at"], "type": row["event_type"], "fields": fields})
    return {"format_version": 1, "application": "Workplace NVR", "generated_at": _now().isoformat(),
            "capture_enabled": until is not None, "capture_until": until.isoformat() if until else None,
            "events": events}
