"""Give fixture camera 1 two footage segments from 40 days ago, with real files, for the storage tiering journeys. Prints their ids and sizes as JSON."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.browser.seed_fixtures import _camera_id, connect  # noqa: F401 - sets up the import path

from app.recording import recordings_path  # noqa: E402


def seed() -> dict[str, object]:
    camera = _camera_id(1)
    made = []
    with connect() as db:
        db.execute("DELETE FROM recording_segments")
        for index in range(2):
            start = datetime.now(timezone.utc) - timedelta(days=40, minutes=index * 2)
            name = f"{uuid.uuid4()}.mp4"
            payload = os.urandom(4096) * 2
            path = recordings_path() / camera / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            segment = db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                                 (camera, f"{camera}/{name}", start.isoformat(), (start + timedelta(seconds=60)).isoformat(), len(payload))).lastrowid
            made.append({"id": segment, "size": len(payload), "camera_id": camera})
    return {"segments": made, "cold": str(Path(os.environ.get("NVR_BROWSER_COLD", "/tmp/nvr-browser-cold")))}


if __name__ == "__main__":
    print(json.dumps(seed()))
