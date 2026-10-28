"""Give fixture camera 1 a shift's worth of events for the digest journeys: three alarms, a recording gap, an incident, and a digest made from them."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from tests.browser.seed_fixtures import _camera_id, connect  # noqa: F401 - sets up the import path

from app import digests  # noqa: E402


def seed() -> dict[str, object]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    camera_id = _camera_id(1)
    at = lambda hours: now - timedelta(hours=hours)  # noqa: E731
    made: dict[str, object] = {"camera_id": camera_id, "alarms": []}
    with connect() as db:
        db.execute("UPDATE cameras SET created_at='2025-01-01T00:00:00+00:00'")
        for table in ("alarms", "rule_events", "rules", "incidents", "recording_segments", "digests", "digest_schedules"):
            db.execute(f"DELETE FROM {table}")
        rule = str(uuid.uuid4())
        db.execute("INSERT INTO rules(id,name,camera_ids_json,condition_type,condition_json,classes_json,active_from) VALUES(?,?,?,?,?,?,?)",
                   (rule, "Front gate", json.dumps([camera_id]), "zone_entry", '{"zone":{"x":0,"y":0,"width":1,"height":1}}', '["person","vehicle"]', at(100).isoformat()))
        for hours, label, state in ((3.5, "person", "resolved"), (2.5, "vehicle", "new"), (1.5, "person", "acknowledged")):
            event, alarm = str(uuid.uuid4()), str(uuid.uuid4())
            when = at(hours).isoformat()
            db.execute("INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json) VALUES(?,?,?,?,?,?,?,?,?)",
                       (event, rule, "Front gate", camera_id, f"t-{event}", label, "zone_entry", when, '{"x":0,"y":0,"width":0.1,"height":0.1}'))
            db.execute("INSERT INTO alarms(id,rule_event_id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json,clip_starts_at,clip_ends_at,state,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (alarm, event, rule, "Front gate", camera_id, f"t-{event}", label, "zone_entry", when, '{"x":0,"y":0,"width":0.1,"height":0.1}',
                                                                  (at(hours) - timedelta(seconds=10)).isoformat(), (at(hours) + timedelta(seconds=20)).isoformat(), state, when, when))
            made["alarms"].append(alarm)
        for start, end in ((at(6), at(3)), (at(2), at(0))):  # A gap of an hour, three to two hours ago
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,100)",
                       (camera_id, f"{camera_id}/{start.timestamp()}.mp4", start.isoformat(), end.isoformat()))
        incident = str(uuid.uuid4())
        db.execute("INSERT INTO incidents(id,incident_key,camera_id,fault_type,summary,state,fault_active,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,'recovered',0,?,?)",
                   (incident, f"key-{incident}", camera_id, "stream_down", "Camera 001 stream down", at(0.75).isoformat(), at(0.7).isoformat()))
        admin = db.execute("SELECT id FROM users WHERE username='camera-admin'").fetchone()[0]
        schedule = str(uuid.uuid4())
        db.execute("INSERT INTO digest_schedules(id,name,scope_json,recipient_user_id,shift_json,after_hours_json,last_period_end) VALUES(?,?,?,?,?,?,?)",
                   (schedule, "Night shift", json.dumps({"site": True}), admin, json.dumps({"days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "08:00"}),
                    json.dumps({"start": "22:00", "end": "06:00"}), now.isoformat()))
        row = db.execute("SELECT * FROM digest_schedules WHERE id=?", (schedule,)).fetchone()
        made["digest_id"] = digests.generate(db, row, at(8), now, now=now)
    return made


if __name__ == "__main__":
    print(json.dumps(seed()))
