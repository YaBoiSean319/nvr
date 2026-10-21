from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.recording import RecordingManager
from app.store import connect, initialize


class CoverageHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_db_path = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        initialize()
        self.camera_id = "coverage-test-camera"
        now = datetime.now(timezone.utc).isoformat()
        with connect() as db:
            db.execute(
                "INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,recording_enabled,recording_status,last_video_at,last_footage_write_at) "
                "VALUES(?,?,X'01','continuous',1,'recording',?,?)",
                (self.camera_id, "Coverage test", now, now),
            )

    def tearDown(self):
        if self.previous_db_path is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous_db_path
        self.temporary.cleanup()

    def test_coverage_transitions_close_prior_interval_and_open_failure_then_recovery(self):
        manager = RecordingManager()
        manager._sync_coverage(self.camera_id)
        with connect() as db:
            db.execute("UPDATE cameras SET last_footage_write_at='2000-01-01T00:00:00+00:00' WHERE id=?", (self.camera_id,))
        manager._sync_coverage(self.camera_id)
        with connect() as db:
            db.execute("UPDATE cameras SET last_footage_write_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (self.camera_id,))
        manager._sync_coverage(self.camera_id)
        with connect() as db:
            rows = db.execute("SELECT coverage_kind,starts_at,ends_at FROM coverage_intervals WHERE camera_id=? ORDER BY id", (self.camera_id,)).fetchall()
        self.assertEqual([row["coverage_kind"] for row in rows], ["covered", "failure", "covered"])
        self.assertTrue(all(row["ends_at"] for row in rows[:-1]))
        self.assertIsNone(rows[-1]["ends_at"])


if __name__ == "__main__":
    unittest.main()
