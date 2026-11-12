"""The camera's analysis state is written when it changes, or every few seconds while it holds (#279), not on every analysed frame."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import detection
from app.store import connect
from tests.scene_case import SceneTestCase

T0 = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)


class StateWriteTests(SceneTestCase):
    def state(self, state: str, seconds: float, error: str | None = None) -> int:
        """Record a state at T0+seconds; returns how many rows the call changed in the status table and the coverage table."""
        with connect() as db:
            before = db.total_changes
            detection._set_camera_state(db, self.camera_id, state, error, at=T0 + timedelta(seconds=seconds))
            return db.total_changes - before

    def row(self):
        with connect() as db:
            return db.execute("SELECT status,last_attempt_at,last_success_at,last_error FROM ai_camera_settings WHERE camera_id=?", (self.camera_id,)).fetchone()

    def test_frames_a_second_apart_write_once_not_every_frame(self):
        writes = [self.state("analyzed", second) for second in range(0, 5)]
        self.assertGreater(writes[0], 0)
        self.assertEqual(writes[1:], [0, 0, 0, 0], "the same state within the interval is not written again")
        self.assertEqual(self.row()["last_success_at"], T0.isoformat())

    def test_the_state_is_refreshed_once_the_interval_has_passed(self):
        self.state("analyzed", 0)
        self.assertEqual(self.state("analyzed", detection.STATE_WRITE_SECONDS - 0.5), 0)
        self.assertGreater(self.state("analyzed", detection.STATE_WRITE_SECONDS + 0.5), 0)
        self.assertEqual(self.row()["last_success_at"], (T0 + timedelta(seconds=detection.STATE_WRITE_SECONDS + 0.5)).isoformat())

    def test_a_change_of_state_is_written_at_once(self):
        self.state("analyzed", 0)
        self.assertGreater(self.state("failed", 1, "The camera stopped"), 0)
        row = self.row()
        self.assertEqual((row["status"], row["last_error"]), ("failed", "The camera stopped"))
        self.assertGreater(self.state("analyzed", 2), 0)  # Recovering is a change too, and clears the error
        self.assertEqual((self.row()["status"], self.row()["last_error"]), ("analyzed", None))

    def test_a_new_error_text_is_written_at_once(self):
        self.state("delayed", 0, "Behind by 3 s")
        self.assertEqual(self.state("delayed", 1, "Behind by 3 s"), self.state("delayed", 1.5, "Behind by 3 s"))
        self.assertGreater(self.state("delayed", 2, "Behind by 9 s"), 0)
        self.assertEqual(self.row()["last_error"], "Behind by 9 s")
