"""Store detections on camera 1 that line up with its generated footage, for the Playback object-track journeys.

Offsets are seconds from the footage anchor. Camera 1 records 0-60 s. Frames were analysed at 4, 6, 8, 10, 12, 14 and 16 s. A person
walks left to right and is hidden behind something for the frames at 10 and 12 s (the same track on both sides of the pause); a parked
vehicle is in every frame.
"""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta

from tests.browser.seed_fixtures import _camera_id, connect  # noqa: F401 - sets up the import path

FRAMES = [4, 6, 8, 10, 12, 14, 16]
PERSON_AT = [4, 6, 8, 14, 16]
PERSON_X = {4: 0.10, 6: 0.22, 8: 0.34, 14: 0.70, 16: 0.82}
VEHICLE = (0.70, 0.62, 0.20, 0.22)  # x, y, width, height


def pose_of(x: float) -> str:
    """The body points the pose model would find for the person whose box is at ``x`` (the box is 0.10 wide and runs from 0.30 to 0.70 down the picture)."""
    points = {"nose": (0.05, 0.33), "left_shoulder": (0.02, 0.40), "right_shoulder": (0.08, 0.40), "left_elbow": (0.0, 0.50), "right_elbow": (0.10, 0.50),
              "left_wrist": (0.0, 0.58), "right_wrist": (0.10, 0.58), "left_hip": (0.03, 0.55), "right_hip": (0.07, 0.55),
              "left_knee": (0.03, 0.65), "right_knee": (0.07, 0.65), "left_ankle": (0.03, 0.70), "right_ankle": (0.07, 0.70)}
    return json.dumps({name: [round(x + dx, 4), dy] for name, (dx, dy) in points.items()}, separators=(",", ":"))


def seed(anchor: str) -> dict[str, object]:
    start = datetime.fromisoformat(anchor)
    camera_id = _camera_id(1)
    person, vehicle = str(uuid.uuid4()), str(uuid.uuid4())
    at = lambda seconds: (start + timedelta(seconds=seconds)).isoformat()  # noqa: E731
    with connect() as db:
        for table in ("ai_detections", "ai_tracks"):
            db.execute(f"DELETE FROM {table} WHERE camera_id=?", (camera_id,))
        for track, label, seen in ((person, "person", PERSON_AT), (vehicle, "vehicle", FRAMES)):
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,?,?,?,'fixture','1','CPU')",
                       (track, camera_id, label, at(seen[0]), at(seen[-1])))
        for seconds in FRAMES:
            if seconds in PERSON_X:
                x = PERSON_X[seconds]
                db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider,pose_json) "
                           "VALUES(?,?,?,?,0.9,?,?,?,?,'fixture','1','CPU',?)", (camera_id, person, at(seconds), "person", x, 0.30, 0.10, 0.40, pose_of(x)))
            x, y, width, height = VEHICLE
            db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                       "VALUES(?,?,?,?,0.8,?,?,?,?,'fixture','1','CPU')", (camera_id, vehicle, at(seconds), "vehicle", x, y, width, height))
    return {"person": person, "vehicle": vehicle, "frames": FRAMES, "person_at": PERSON_AT,
            "person_x": {str(key): value for key, value in PERSON_X.items()}, "camera_id": camera_id}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--anchor", required=True)
    print(json.dumps(seed(parser.parse_args().anchor)))
