"""Give fixture cameras a history of writing, and the disk a SMART reading, while a journey runs."""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone

from tests.browser.seed_fixtures import _camera_id, connect  # noqa: F401 - sets up the import path

from app import smart  # noqa: E402

TB = 10 ** 12


def write_history(camera: int, tb_per_day: float, hours: int = 48) -> dict[str, object]:
    """Hourly segments over the last `hours` that add up to `tb_per_day` a day; far more than any test disk holds, so the forecast falls short."""
    camera_id = _camera_id(camera)
    now = datetime.now(timezone.utc)
    with connect() as db:
        db.execute("DELETE FROM recording_segments WHERE camera_id=?", (camera_id,))
        for hour in range(hours):
            start = now - timedelta(hours=hours - hour)
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                       (camera_id, f"{camera_id}/{uuid.uuid4()}.mp4", start.isoformat(), (start + timedelta(hours=1)).isoformat(), int(tb_per_day * TB / 24)))
    return {"camera_id": camera_id}


def smart_reading(state: str) -> dict[str, object]:
    reports = {
        "failing": {"model_name": "Fixture HDD 4TB", "smart_status": {"passed": False}, "temperature": {"current": 38}, "power_on_time": {"hours": 31000},
                    "ata_smart_attributes": {"table": [{"id": 197, "name": "Current_Pending_Sector", "value": 90, "worst": 90, "thresh": 0, "when_failed": "", "raw": {"value": 24}}]}},
        "healthy": {"model_name": "Fixture HDD 4TB", "smart_status": {"passed": True}, "temperature": {"current": 33}, "power_on_time": {"hours": 900}, "ata_smart_attributes": {"table": []}},
    }
    assessment = smart.assess(reports[state])
    with connect() as db:
        smart.record(db, "default", "/dev/sda", assessment, None)
    return {"state": assessment.state}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="what", required=True)
    history = sub.add_parser("history")
    history.add_argument("--camera", type=int, required=True)
    history.add_argument("--tb-per-day", type=float, required=True)
    reading = sub.add_parser("smart")
    reading.add_argument("--state", choices=["failing", "healthy"], required=True)
    options = parser.parse_args()
    print(json.dumps(write_history(options.camera, options.tb_per_day) if options.what == "history" else smart_reading(options.state)))


if __name__ == "__main__":
    main()
