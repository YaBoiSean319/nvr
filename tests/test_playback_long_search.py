"""A Playback search shows all of its footage, however many recorded files it spans (#360).

The timeline read at most 500 segments and worked out the gaps from only those, so a day of one-minute files showed footage up to 08:20 and a
false "coverage is uncertain" gap for the rest of the day."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.store import connect
from tests.test_access_grants import GrantCase

DAY = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)


class LongSearchTests(GrantCase):
    def test_a_whole_day_of_one_minute_files_is_all_there_with_no_false_gap(self):
        with connect() as db:
            db.execute("BEGIN")
            db.executemany("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,1000)",
                           ((self.d1, f"{self.d1}/day/segment-{minute:04d}.mp4", (DAY + timedelta(minutes=minute)).isoformat(), (DAY + timedelta(minutes=minute + 1)).isoformat())
                            for minute in range(1440)))
            db.execute("COMMIT")
        response = self.admin.post("/api/playback/timeline", json={"camera_ids": [self.d1], "start": DAY.isoformat(), "end": (DAY + timedelta(days=1)).isoformat()})
        self.assertEqual(response.status_code, 200, response.text)
        camera = response.json()["cameras"][0]
        self.assertEqual(len(camera["segments"]), 1440)
        self.assertEqual(camera["segments"][-1]["starts_at"], (DAY + timedelta(minutes=1439)).isoformat())
        self.assertEqual(camera["gaps"], [], "footage that exists was reported as a gap")
