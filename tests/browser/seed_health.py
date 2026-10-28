"""Give fixture cameras 1-3 health values for the dashboard journey: two of one model on different firmware, the third silent, and recorder counters."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tests.browser.seed_fixtures import _camera_id, connect  # noqa: F401 - sets up the import path


def seed() -> dict[str, object]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with connect() as db:
        db.execute("DELETE FROM metric_counters")
        for index in (1, 2):
            db.execute("UPDATE cameras SET recording_status='recording' WHERE id=?", (_camera_id(index),))  # The recorder's state first: its trigger starts the uptime clock
        db.execute("DELETE FROM camera_health")
        db.execute("DELETE FROM metric_counters")
        for index, (maker, model, firmware, bitrate, fps, up_hours) in enumerate(((("Acme", "Cam-X1", "2.10.0", 4096, 15, 30), ("Acme", "Cam-X1", "2.9.1", 2048, 12, 5)))[:2], start=1):
            db.execute("INSERT INTO camera_health(camera_id,manufacturer,model,firmware,configured_codec,configured_fps,configured_bitrate_kbps,onvif_contact_at,onvif_checked_at,measured_fps,measured_bitrate_kbps,measured_at,stream_up_since) "
                       "VALUES(?,?,?,?,'h264',?,?,?,?,?,?,?,?)", (_camera_id(index), maker, model, firmware, fps, bitrate, now.isoformat(), now.isoformat(), fps - 1, bitrate * 0.9, now.isoformat(),
                                                                    (now - timedelta(hours=up_hours)).isoformat()))
        db.execute("INSERT INTO metric_counters(name,camera_id,value) VALUES('frames_dropped',?,7),('reconnects',?,3)", (_camera_id(1), _camera_id(1)))
    return {"cameras": [_camera_id(index) for index in (1, 2, 3)]}


if __name__ == "__main__":
    print(json.dumps(seed()))
