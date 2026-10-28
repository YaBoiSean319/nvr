"""Put a person in view of fixture camera 1 right now (a track and its newest detection), off to the right of the picture, for the PTZ auto-tracking journeys."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from tests.browser.seed_fixtures import _camera_id, connect  # noqa: F401 - sets up the import path


def seed() -> dict[str, str]:
    camera = _camera_id(1)
    track = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("DELETE FROM ai_tracks WHERE camera_id=?", (camera,))
        db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,detection_count,max_confidence,model_id,model_version,provider) VALUES(?,?,'person',?,?,1,0.9,'fixture','1','cpu')",
                   (track, camera, now, now))
        db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                   "VALUES(?,?,?,'person',0.9,0.7,0.3,0.1,0.35,'fixture','1','cpu')", (camera, track, now))
    return {"track_id": track, "camera_id": camera}


if __name__ == "__main__":
    print(json.dumps(seed()))
