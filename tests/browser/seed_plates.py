"""Give fixture cameras vehicle tracks with plate reads (and their crops) for the licence plate journeys."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from tests.browser.seed_fixtures import _camera_id, connect  # noqa: F401 - sets up the import path

import numpy as np  # noqa: E402

from app import plates  # noqa: E402


def seed() -> dict[str, object]:
    """Four reads on cameras 1 and 2 within the last day: AB12CDE twice (one is O for 0 away from a look-alike), KT55QRS and an unrelated plate."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    specs = [("AB12CDE", 1, now - timedelta(hours=1), 0.93), ("AB1ZCDE", 2, now - timedelta(hours=2), 0.71), ("KT55QRS", 1, now - timedelta(hours=3), 0.88), ("ZZ99ZZZ", 2, now - timedelta(hours=5), 0.64)]
    made: dict[str, object] = {}
    crop = np.full((24, 94, 3), 235, dtype=np.uint8)
    crop[6:18, 8:86] = 40
    jpeg = plates.encode_crop(crop)
    if jpeg is None:
        raise RuntimeError("FFmpeg is needed to make the fixture pictures")
    with connect() as db:
        for table in ("plate_reads", "ai_detections", "ai_tracks"):
            db.execute(f"DELETE FROM {table}")
        for text, camera, at, confidence in specs:
            track, camera_id = str(uuid.uuid4()), _camera_id(camera)
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,'vehicle',?,?,'fixture','1','CPU')", (track, camera_id, at.isoformat(), at.isoformat()))
            db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) VALUES(?,?,?,'vehicle',0.9,0.3,0.35,0.3,0.35,'fixture','1','CPU')",
                       (camera_id, track, at.isoformat()))
            read_id, _ = plates.store_read(db, camera_id, track, text, confidence, at, {"x": 0.4, "y": 0.6, "width": 0.1, "height": 0.04}, jpeg, "fixture")
            made[text] = {"read_id": read_id, "track_id": track, "camera_id": camera_id, "read_at": at.isoformat()}
    return made


def read_now(text: str, camera: int) -> dict[str, object]:
    """A vehicle seen this moment with this plate, as the AI worker would store it: rules that watch the plate fire now."""
    now = datetime.now(timezone.utc)
    track, camera_id = str(uuid.uuid4()), _camera_id(camera)
    with connect() as db:
        db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,'vehicle',?,?,'fixture','1','CPU')", (track, camera_id, now.isoformat(), now.isoformat()))
        db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) VALUES(?,?,?,'vehicle',0.9,0.3,0.35,0.3,0.35,'fixture','1','CPU')",
                   (camera_id, track, now.isoformat()))
        db.execute("BEGIN IMMEDIATE")
        read_id, _ = plates.store_read(db, camera_id, track, text, 0.9, now, {"x": 0.4, "y": 0.6, "width": 0.1, "height": 0.04}, None, "fixture")
        db.execute("COMMIT")
    return {"read_id": read_id, "track_id": track}


if __name__ == "__main__":
    import sys

    print(json.dumps(read_now(sys.argv[2], int(sys.argv[3])) if len(sys.argv) == 4 and sys.argv[1] == "read" else seed()))
