from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(db: sqlite3.Connection, incident_id: str, event_type: str, details: dict[str, Any], actor_id: int | None = None) -> None:
    event_id = str(uuid.uuid4())
    occurred_at = _now()
    db.execute(
        "INSERT INTO incident_events(event_id,incident_id,event_type,actor_id,occurred_at,details_json) VALUES(?,?,?,?,?,?)",
        (event_id, incident_id, event_type, actor_id, occurred_at, json.dumps(details, sort_keys=True)),
    )
    if event_type not in {"raised", "recurred", "recovered"}:
        return
    incident = db.execute("SELECT fault_type,camera_id,summary,recurrence_count FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if incident is None:
        return
    category = "storage" if incident["fault_type"] == "storage" else "recording"
    if event_type == "recovered":
        webhook_type = f"{category}.recovered"
    elif incident["fault_type"] == "recording_uncertain":
        webhook_type = "recording.uncertain"
    else:
        webhook_type = f"{category}.failure"
    from .webhooks import enqueue_event
    enqueue_event(db, webhook_type, {
        "event_id": event_id,
        "event_type": webhook_type,
        "occurred_at": occurred_at,
        "incident_id": incident_id,
        "fault_type": incident["fault_type"],
        "camera_id": incident["camera_id"],
        "summary": incident["summary"],
        "recurrence_count": int(incident["recurrence_count"]),
        "details": details,
    })


def sync_incident(
    db: sqlite3.Connection,
    *,
    incident_key: str,
    camera_id: str | None,
    fault_type: str,
    summary: str,
    active: bool,
    details: dict[str, Any] | None = None,
) -> None:
    """Persist a fault observation; repeated observations retain stable incident identity."""
    details_json = json.dumps(details or {}, sort_keys=True)
    row = db.execute("SELECT * FROM incidents WHERE incident_key=?", (incident_key,)).fetchone()
    now = _now()
    if active:
        if row is None:
            incident_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO incidents(id,incident_key,camera_id,fault_type,summary,state,fault_active,recurrence_count,first_seen_at,last_seen_at,details_json) "
                "VALUES(?,?,?,?,?,'active',1,1,?,?,?)",
                (incident_id, incident_key, camera_id, fault_type, summary, now, now, details_json),
            )
            _event(db, incident_id, "raised", details or {})
        elif row["state"] == "recovered" or not row["fault_active"]:
            db.execute(
                "UPDATE incidents SET camera_id=?,fault_type=?,summary=?,state='active',fault_active=1,recurrence_count=recurrence_count+1,last_seen_at=?,acknowledged_by=NULL,acknowledged_at=NULL,recovered_at=NULL,details_json=? WHERE id=?",
                (camera_id, fault_type, summary, now, details_json, row["id"]),
            )
            _event(db, row["id"], "recurred", details or {})
        else:
            db.execute(
                "UPDATE incidents SET summary=?,last_seen_at=?,details_json=? WHERE id=?",
                (summary, now, details_json, row["id"]),
            )
    elif row is not None and row["fault_active"]:
        db.execute(
            "UPDATE incidents SET state='recovered',fault_active=0,last_seen_at=?,recovered_at=?,details_json=? WHERE id=?",
            (now, now, details_json, row["id"]),
        )
        _event(db, row["id"], "recovered", details or {})


def acknowledge_incident(db: sqlite3.Connection, incident_id: str, actor_id: int) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if row is None:
        return None
    if not row["fault_active"]:
        return {**dict(row), "acknowledgement_conflict": True}
    if row["state"] != "acknowledged":
        now = _now()
        db.execute(
            "UPDATE incidents SET state='acknowledged',acknowledged_by=?,acknowledged_at=? WHERE id=? AND fault_active=1",
            (actor_id, now, incident_id),
        )
        _event(db, incident_id, "acknowledged", {}, actor_id)
    return dict(db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone())


def incident_view(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["fault_active"] = bool(result["fault_active"])
    result["details"] = json.loads(result.pop("details_json"))
    result["events"] = []
    for event in db.execute("SELECT * FROM incident_events WHERE incident_id=? ORDER BY id", (row["id"],)).fetchall():
        event_data = dict(event)
        event_data["details"] = json.loads(event_data.pop("details_json"))
        result["events"].append(event_data)
    return result


def list_incidents(db: sqlite3.Connection, state: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if state is None:
        rows = db.execute("SELECT * FROM incidents ORDER BY last_seen_at DESC LIMIT ?", (limit,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM incidents WHERE state=? ORDER BY last_seen_at DESC LIMIT ?", (state, limit)).fetchall()
    return [incident_view(db, row) for row in rows]


def sync_storage_incidents(db: sqlite3.Connection, volumes: list[dict[str, Any]]) -> None:
    """Group an unhealthy recording volume into one incident with its affected cameras."""
    observed: set[str] = set()
    unhealthy_states = {"pressure", "missing", "read_only", "unavailable", "slow", "unconfigured"}
    for volume in volumes:
        volume_id = str(volume["id"])
        key = f"storage:{volume_id}"
        observed.add(key)
        affected = volume.get("cameras", [])
        state = str(volume.get("state", "unavailable"))
        sync_incident(
            db,
            incident_key=key,
            camera_id=None,
            fault_type="storage",
            summary=f"Recording storage {volume_id} is {state}; {len(affected)} camera(s) are assigned",
            active=state in unhealthy_states and bool(affected),
            details={"volume_id": volume_id, "volume_state": state, "affected_cameras": affected},
        )
    rows = db.execute("SELECT incident_key FROM incidents WHERE fault_type='storage' AND fault_active=1").fetchall()
    for row in rows:
        if row["incident_key"] not in observed:
            sync_incident(
                db,
                incident_key=row["incident_key"],
                camera_id=None,
                fault_type="storage",
                summary="Recording storage is no longer configured",
                active=False,
                details={"volume_state": "removed"},
            )
