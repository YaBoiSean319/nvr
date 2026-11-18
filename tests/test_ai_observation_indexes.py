"""AI observations are found through indexes and follow their camera's retention (#345).

Every analysed frame looked for a recent observation of each object by reading every observation the camera had ever stored, inside the frame's
write transaction; nothing removed an observation."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.detection import _record_observations
from app.retention import prune_ai_observations
from app.store import connect
from tests.test_headroom import HeadroomCase

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
BOX = {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.4}


class ObservationTests(HeadroomCase):
    def observe(self, name: str, camera: str, *, days_ago: float) -> None:
        at = (NOW - timedelta(days=days_ago)).isoformat()
        with connect() as db:
            db.execute("INSERT INTO ai_observations(id,camera_id,label,confidence,box_json,first_seen_at,last_seen_at,frame_width,frame_height,model_id,model_version,provider) "
                       "VALUES(?,?,'person',0.9,?,?,?,448,256,'m','1','CPU')", (name, self.cameras[camera], json.dumps(BOX), at, at))

    def stored(self) -> set[str]:
        with connect() as db:
            return {row[0] for row in db.execute("SELECT id FROM ai_observations")}

    def plan(self, sql: str, *params: object) -> str:
        with connect() as db:
            return " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params))

    def test_the_per_frame_lookup_seeks_the_last_few_seconds_and_does_not_sort(self):
        plan = self.plan("SELECT id,box_json FROM ai_observations WHERE camera_id=? AND label=? AND model_id=? AND model_version=? AND provider=? AND last_seen_at>=? "
                         "ORDER BY last_seen_at DESC LIMIT 100", "c", "person", "m", "1", "CPU", "a")
        self.assertIn("ai_observations_recent_idx", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_the_timeline_reads_a_cameras_observations_in_time_order_from_an_index(self):
        plan = self.plan("SELECT id FROM ai_observations WHERE camera_id=? AND first_seen_at>=? AND first_seen_at<? ORDER BY first_seen_at LIMIT 1000", "c", "a", "b")
        self.assertIn("ai_observations_camera_time_idx", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_storing_a_frame_does_not_read_the_cameras_old_observations(self):
        with connect() as db:
            db.execute("BEGIN")
            db.executemany("INSERT INTO ai_observations(id,camera_id,label,confidence,box_json,first_seen_at,last_seen_at,frame_width,frame_height,model_id,model_version,provider) "
                           "VALUES(?,?,'person',0.9,?,?,?,448,256,'m','1','CPU')",
                           ((f"old-{number}", self.cameras["north"], json.dumps(BOX), (NOW - timedelta(days=5, seconds=number)).isoformat(),
                             (NOW - timedelta(days=5, seconds=number)).isoformat()) for number in range(20000)))
            db.execute("COMMIT")
            steps = 0

            def count() -> int:
                nonlocal steps
                steps += 1
                return 0

            db.set_progress_handler(count, 1000)  # A read of 20,000 rows and a sort take hundreds of thousands of steps
            stored = _record_observations(db, self.cameras["north"], [{"label": "person", "confidence": 0.9, "box": BOX}], NOW, "m", "1", "CPU")
            db.set_progress_handler(None, 0)
        self.assertEqual(stored, 1)
        self.assertLess(steps, 20, "storing one observation read the camera's whole history")

    def test_observations_past_the_cameras_policy_with_no_footage_are_removed(self):
        with connect() as db:
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,7)", (self.cameras["north"],))
        self.observe("old", "north", days_ago=8)
        self.observe("recent", "north", days_ago=6)
        self.observe("other-camera", "south", days_ago=8)  # Inside the default policy
        self.assertEqual(prune_ai_observations(NOW), 1)
        self.assertEqual(self.stored(), {"recent", "other-camera"})
        self.assertEqual(prune_ai_observations(NOW), 0)

    def test_an_old_observation_inside_footage_that_is_still_kept_stays(self):
        with connect() as db:
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,7)", (self.cameras["north"],))
        self.segment("north", hours_ago=24 * 8)  # Ten minutes of footage ending eight days ago, still on disk (a hold, say)
        self.observe("covered", "north", days_ago=8 + 5 / (24 * 60))
        self.assertEqual(prune_ai_observations(NOW), 0)
        self.assertEqual(self.stored(), {"covered"})
