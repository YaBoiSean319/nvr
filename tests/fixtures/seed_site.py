"""Seed one fixture NVR (a site, run with that site's environment): cameras with addresses, a recording, an alarm and a track. Prints what it made as JSON.

    python -m tests.fixtures.seed_site '{"cameras": ["Gate A1", "Gate A2"], "group": "Gates"}'
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import alarms
from app.credentials import encrypt
from app.store import connect


def main(spec: dict) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    made: dict = {"cameras": {}, "alarm": None, "track": None, "segment": None}
    group = str(uuid.uuid4())
    root = Path(os.environ["NVR_RECORDINGS_PATH"])
    with connect() as db:
        db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)", (group, spec.get("group", "Cameras")))
        for index, name in enumerate(spec["cameras"]):
            camera = str(uuid.uuid4())
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,group_id,recording_mode,recording_enabled,camera_ip,device_service_url) VALUES(?,?,?,?,'continuous',1,?,?)",
                       (camera, name, encrypt("rtsp://viewer:secret-pass@camera.invalid/main"), group, f"10.77.{index}.9", f"http://10.77.{index}.9/onvif/device_service"))
            made["cameras"][name] = camera
        first = next(iter(made["cameras"].values()))
        payload = bytes(range(256)) * 8
        path = root / first / "fixture.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        starts = now - timedelta(minutes=10)
        segment = db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                             (first, f"{first}/fixture.mp4", starts.isoformat(), (starts + timedelta(minutes=2)).isoformat(), len(payload))).lastrowid
        made["segment"] = {"id": segment, "size": len(payload)}
        rule = str(uuid.uuid4())
        db.execute("INSERT INTO rules(id,name,camera_ids_json,condition_type,condition_json,classes_json,active_from) VALUES(?,?,?,?,?,?,?)",
                   (rule, spec.get("rule", "Gate entry"), json.dumps([first]), "zone_entry", '{"zone":{"x":0,"y":0,"width":1,"height":1}}', '["vehicle"]', (now - timedelta(hours=1)).isoformat()))
        event = str(uuid.uuid4())
        occurred = starts + timedelta(seconds=30)
        db.execute("INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json) VALUES(?,?,?,?,?,?,?,?,?)",
                   (event, rule, spec.get("rule", "Gate entry"), first, "t", "vehicle", "zone_entry", occurred.isoformat(), '{"x":0,"y":0,"width":0.1,"height":0.1}'))
        made["alarm"] = alarms.create_alarm(db, event)
        track = str(uuid.uuid4())
        db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider,upper_colour) VALUES(?,?,'person',?,?,'m','1','CPU','red')",
                   (track, first, occurred.isoformat(), occurred.isoformat()))
        db.execute("INSERT INTO track_keyframes(track_id,captured_at,jpeg) VALUES(?,?,?)", (track, occurred.isoformat(), b"\xff\xd8fixture-picture\xff\xd9"))
        made["track"] = track
    return made


if __name__ == "__main__":
    print(json.dumps(main(json.loads(sys.argv[1]))))
