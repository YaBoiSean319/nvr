"""Motion events are indexed for their readers and go with their footage (#333).

Deleting a segment used to read every motion event ever recorded, once per segment, inside the write transaction; nothing removed a motion event."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.migrations import m0065_motion_event_indexes
from app.retention import prune_motion_events
from app.store import connect
from tests.test_headroom import HeadroomCase

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


class MotionEventTests(HeadroomCase):
    def event(self, camera: str, *, days_ago: float, status: str = "complete", segment_id: int | None = None) -> str:
        at = (NOW - timedelta(days=days_ago)).isoformat()
        event_id = str(uuid.uuid4())
        with connect() as db:
            db.execute(
                "INSERT INTO motion_events(id,camera_id,source,source_topic,source_event_at,received_at,requested_start,requested_end,status,segment_id) "
                "VALUES(?,?,'onvif','motion',?,?,?,?,?,?)", (event_id, self.cameras[camera], at, at, at, at, status, segment_id))
        return event_id

    def events(self) -> set[str]:
        with connect() as db:
            return {row[0] for row in db.execute("SELECT id FROM motion_events")}

    def plan(self, sql: str, *params: object) -> str:
        with connect() as db:
            return " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params))

    def test_deleting_a_segment_removes_its_motion_event_and_no_other(self):
        first, second = self.segment("north", 5), self.segment("north", 4)
        mine, other, failed = self.event("north", days_ago=1, segment_id=first), self.event("north", days_ago=1, segment_id=second), self.event("north", days_ago=1, status="failed")
        with connect() as db:
            db.execute("DELETE FROM recording_segments WHERE id=?", (first,))
        self.assertEqual(self.events(), {other, failed})
        self.assertNotIn(mine, self.events())

    def test_finding_a_segments_motion_events_is_a_seek_not_a_read_of_the_table(self):
        for sql in ("DELETE FROM motion_events WHERE segment_id=?", "UPDATE motion_events SET segment_id=NULL WHERE segment_id=?"):
            plan = self.plan(sql, 1)
            self.assertIn("motion_events_segment_idx", plan, sql)
            self.assertNotIn("SCAN", plan, sql)

    def test_the_timeline_reads_a_cameras_events_in_time_order_from_an_index(self):
        plan = self.plan("SELECT id FROM motion_events WHERE camera_id=? AND source_event_at>=? AND source_event_at<? ORDER BY source_event_at LIMIT 1000", "c", "a", "b")
        self.assertIn("motion_events_camera_event_idx", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_the_cost_of_deleting_a_segment_does_not_grow_with_the_motion_events_table(self):
        segment = self.segment("north", 5)
        with connect() as db:
            db.execute("BEGIN")
            db.executemany(
                "INSERT INTO motion_events(id,camera_id,source,source_topic,source_event_at,received_at,requested_start,requested_end,status) VALUES(?,?,'onvif','motion','a','a','a','a','failed')",
                ((f"filler-{number}", self.cameras["south"]) for number in range(20000)))
            db.execute("COMMIT")
            steps = 0

            def count() -> int:
                nonlocal steps
                steps += 1
                return 0

            db.set_progress_handler(count, 1000)  # Called every 1,000 virtual-machine steps: a scan of 20,000 rows takes hundreds of thousands
            db.execute("DELETE FROM recording_segments WHERE id=?", (segment,))
            db.set_progress_handler(None, 0)
        self.assertLess(steps, 20, "deleting one segment read the motion events table")

    def test_events_whose_footage_was_already_removed_are_cleared_once_by_the_migration(self):
        segment = self.segment("north", 5)
        kept, orphan, failed = self.event("north", days_ago=1, segment_id=segment), self.event("north", days_ago=1), self.event("north", days_ago=1, status="failed")
        recording = self.event("north", days_ago=0, status="recording")
        with connect() as db:
            m0065_motion_event_indexes.upgrade(db)
        self.assertEqual(self.events(), {kept, failed, recording})
        self.assertNotIn(orphan, self.events())

    def test_events_without_footage_are_kept_for_the_cameras_retention_period_and_no_longer(self):
        with connect() as db:
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,7)", (self.cameras["north"],))
        old, recent = self.event("north", days_ago=8, status="failed"), self.event("north", days_ago=6, status="failed")
        recording = self.event("north", days_ago=9, status="recording")
        self.assertEqual(prune_motion_events(NOW), 1)
        self.assertEqual(self.events(), {recent, recording})
        self.assertNotIn(old, self.events())
        self.assertEqual(prune_motion_events(NOW), 0)
