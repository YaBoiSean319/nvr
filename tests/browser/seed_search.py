"""Give fixture cameras tracks with known attributes, keyframe pictures and analysis coverage, for the attribute search journeys."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from tests.browser.seed_fixtures import _camera_id, connect  # noqa: F401 - sets up the import path

from app import attributes  # noqa: E402
from app.tracks import encode_jpeg  # noqa: E402
from tests.fixtures import detection_scene as scene  # noqa: E402


def seed() -> dict[str, object]:
    """Four tracks on cameras 1-3 within the last day, and coverage that leaves camera 1 a failed hour and camera 3 never analysed."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    jpeg = encode_jpeg(scene.frame(0))
    if jpeg is None:
        raise RuntimeError("FFmpeg is needed to make the fixture pictures")
    specs = [
        ("red-walker", 1, "person", now - timedelta(hours=3), {"upper_colour": "red", "lower_colour": "blue", "direction": "right"}, [(0.2, 0.4), (0.8, 0.4)]),
        ("green-walker", 1, "person", now - timedelta(hours=2), {"upper_colour": "green", "lower_colour": "black", "direction": "left"}, [(0.8, 0.4), (0.2, 0.4)]),
        ("red-truck", 2, "vehicle", now - timedelta(hours=4), {"vehicle_colour": "red", "vehicle_type": "truck", "direction": "toward"}, [(0.5, 0.3), (0.5, 0.8)]),
        ("far-walker", 3, "person", now - timedelta(hours=1), {"upper_colour": "red", "lower_colour": "blue", "direction": "left"}, [(0.7, 0.4), (0.3, 0.4)]),
    ]
    made: dict[str, object] = {}
    with connect() as db:
        for table in ("track_keyframes", "ai_detections", "ai_tracks", "ai_coverage"):
            db.execute(f"DELETE FROM {table}")
        for name, camera, label, at, found, path in specs:
            track = str(uuid.uuid4())
            camera_id = _camera_id(camera)
            mask = 0
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,?,?,?,'fixture','1','CPU')", (track, camera_id, label, at.isoformat(), at.isoformat()))
            for index, (cx, cy) in enumerate(path):
                box = {"x": cx - 0.03, "y": cy - 0.12, "width": 0.06, "height": 0.24}
                mask |= attributes.cell_mask(box)
                db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) VALUES(?,?,?,?,0.9,?,?,?,?,'fixture','1','CPU')",
                           (camera_id, track, (at + timedelta(seconds=8 * index)).isoformat(), label, box["x"], box["y"], box["width"], box["height"]))
            db.execute("UPDATE ai_tracks SET upper_colour=?,lower_colour=?,vehicle_colour=?,vehicle_type=?,direction=?,zone_mask=? WHERE id=?",
                       (found.get("upper_colour"), found.get("lower_colour"), found.get("vehicle_colour"), found.get("vehicle_type"), found.get("direction"), mask, track))
            db.execute("INSERT INTO track_keyframes(track_id,captured_at,jpeg) VALUES(?,?,?)", (track, at.isoformat(), jpeg))
            made[name] = {"track_id": track, "camera_id": camera_id, "first_seen_at": at.isoformat()}
        week = (now - timedelta(days=7)).isoformat()
        # Coverage that has an end, far ahead: the AI supervisor closes any open-ended row for a camera whose AI is off (every few seconds), which would
        # leave the end of the searched range "not running" and make the journey depend on how fast it runs.
        later = (now + timedelta(days=365)).isoformat()
        db.execute("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at) VALUES(?,'analyzed',?,?)", (_camera_id(1), week, (now - timedelta(hours=9)).isoformat()))
        db.execute("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at,detail) VALUES(?,'failed',?,?,?)", (_camera_id(1), (now - timedelta(hours=9)).isoformat(), (now - timedelta(hours=8)).isoformat(), "Camera stream ended"))
        db.execute("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at) VALUES(?,'analyzed',?,?)", (_camera_id(1), (now - timedelta(hours=8)).isoformat(), later))
        db.execute("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at) VALUES(?,'analyzed',?,?)", (_camera_id(2), week, later))
    return made


if __name__ == "__main__":
    print(json.dumps(seed()))
