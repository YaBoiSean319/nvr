from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.display_settings import format_display_datetime, local_time_candidates
from app.main import app, current_user
from app.store import connect, initialize


class DisplaySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH")}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.temporary.name) / "recordings")
        initialize()
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(2,'operator','unused','operator')")
        self.active_user = {"id": 1, "username": "admin", "role": "administrator"}
        app.dependency_overrides[current_user] = lambda: self.active_user
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        app.dependency_overrides.pop(current_user, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def test_admin_preview_and_save_validated_site_display_preferences(self):
        preview = self.client.post("/api/settings/display/preview", json={
            "time_zone": "America/New_York", "date_format": "iso", "clock_format": "24",
        })
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["preview"], "2026-10-21 12:05:06")

        saved = self.client.put("/api/settings/display", json={
            "time_zone": "America/New_York", "date_format": "day-month", "clock_format": "24",
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["date_format"], "day-month")
        self.assertEqual(self.client.get("/api/settings/display").json()["clock_format"], "24")

    def test_display_settings_reject_unknown_zone_and_unsupported_formats(self):
        for payload in (
            {"time_zone": "Not/A_Zone", "date_format": "friendly", "clock_format": "12"},
            {"time_zone": "UTC", "date_format": "%Y-%m-%d", "clock_format": "12"},
            {"time_zone": "UTC", "date_format": "friendly", "clock_format": "24h"},
        ):
            self.assertEqual(self.client.put("/api/settings/display", json=payload).status_code, 422)

    def test_operator_can_read_but_cannot_preview_or_change_site_display_preferences(self):
        self.active_user = {"id": 2, "username": "operator", "role": "operator"}
        self.assertEqual(self.client.get("/api/settings/display").status_code, 200)
        payload = {"time_zone": "UTC", "date_format": "friendly", "clock_format": "12"}
        self.assertEqual(self.client.post("/api/settings/display/preview", json=payload).status_code, 403)
        self.assertEqual(self.client.put("/api/settings/display", json=payload).status_code, 403)

    def test_dst_local_time_candidates_detect_gaps_and_both_fold_occurrences(self):
        missing = local_time_candidates(datetime(2026, 3, 8, 2, 30), "America/New_York")
        repeated = local_time_candidates(datetime(2026, 11, 1, 1, 30), "America/New_York")
        self.assertEqual(missing, [])
        self.assertEqual(len(repeated), 2)
        self.assertEqual((repeated[1] - repeated[0]).total_seconds(), 3600)

    def test_date_and_clock_formats_use_site_zone_without_changing_utc_instant(self):
        value = datetime.fromisoformat("2026-11-01T05:30:06+00:00")
        formatted = format_display_datetime(value, "America/New_York", "long", "24")
        self.assertEqual(formatted, "November 1, 2026 01:30:06")


if __name__ == "__main__":
    unittest.main()
