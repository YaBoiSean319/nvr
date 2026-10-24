"""Raise a new alarm from a pop-in rule while a journey runs, as the rules worker would."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
import uuid

from tests.browser.seed_fixtures import NAMESPACE, _camera_id, connect  # noqa: F401 - sets up the import path

from app.alarms import create_alarm  # noqa: E402


def raise_alarm(camera: int, neighbours: list[int], rule_name: str) -> dict[str, object]:
    camera_id = _camera_id(camera)
    rule_id = str(uuid.uuid5(NAMESPACE, f"pop-in-rule-{rule_name}"))
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO rules(id,name,camera_ids_json,condition_type,condition_json,classes_json,active_from,pop_into_live) VALUES(?,?,?,?,?,?,?,1)",
            (rule_id, rule_name, json.dumps([camera_id]), "zone_entry", json.dumps({"zone": {"x": 0, "y": 0, "width": 1, "height": 1}}), '["person"]',
             datetime.now(timezone.utc).isoformat()),
        )
        db.execute("DELETE FROM camera_neighbours WHERE camera_id=?", (camera_id,))
        db.executemany("INSERT INTO camera_neighbours(camera_id,neighbour_id,position) VALUES(?,?,?)",
                       [(camera_id, _camera_id(index), position) for position, index in enumerate(neighbours)])
        event_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, rule_id, rule_name, camera_id, f"track-{event_id}", "person", "zone_entry", datetime.now(timezone.utc).isoformat(),
             '{"x":0.4,"y":0.4,"width":0.2,"height":0.3}'),
        )
        alarm_id = create_alarm(db, event_id)
    return {"alarm_id": alarm_id, "camera_id": camera_id, "neighbour_ids": [_camera_id(index) for index in neighbours]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", type=int, required=True)
    parser.add_argument("--neighbours", default="", help="comma-separated camera indexes")
    parser.add_argument("--rule", default="Gate entry")
    options = parser.parse_args()
    neighbours = [int(item) for item in options.neighbours.split(",") if item]
    print(json.dumps(raise_alarm(options.camera, neighbours, options.rule)))


if __name__ == "__main__":
    main()
