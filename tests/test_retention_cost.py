"""The retention pass looks only at footage old enough to be removed (#337).

It used to load every stored segment and take the database's write lock once for each, expired or not, every five minutes."""
from __future__ import annotations

from unittest.mock import patch

from app import retention
from app.store import connect
from tests.test_headroom import NOW, HeadroomCase


class RetentionCostTests(HeadroomCase):
    def run_pass(self) -> tuple[dict[str, int], int]:
        """(the pass's result, how many segments it took the write lock for)."""
        with patch.object(retention, "remove_segment", wraps=retention.remove_segment) as considered:
            return retention.cleanup_expired_recordings(NOW), considered.call_count

    def test_footage_inside_its_policy_is_not_locked_or_loaded(self):
        for hour in range(1, 201):
            self.segment("north" if hour % 2 else "south", hours_ago=hour)
        result, considered = self.run_pass()
        self.assertEqual((result["removed_segments"], result["failed"], considered), (0, 0, 0))
        with connect() as db:
            self.assertEqual(len(retention._expired_candidates(db, NOW)), 0)

    def test_only_the_expired_segments_are_considered_and_they_are_removed(self):
        young = [self.segment("north", hours_ago=hour) for hour in range(1, 50)]
        old = [self.segment("south", hours_ago=24 * 400 + hour) for hour in range(3)]
        result, considered = self.run_pass()
        self.assertEqual((result["removed_segments"], considered), (3, 3))
        self.assertEqual(sorted(self.remaining()), sorted(young))
        self.assertTrue(all(segment not in self.remaining() for segment in old))

    def test_each_cameras_own_policy_decides_what_is_old_enough(self):
        with connect() as db:
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,2)", (self.cameras["north"],))
        short = self.segment("north", hours_ago=72)   # Past north's two days
        long = self.segment("south", hours_ago=72)    # Inside the default policy
        result, considered = self.run_pass()
        self.assertEqual((result["removed_segments"], considered), (1, 1))
        self.assertEqual(self.remaining(), [long])
        self.assertNotIn(short, self.remaining())

    def test_expired_footage_under_a_hold_is_left_out_without_a_lock(self):
        held = self.segment("north", hours_ago=24 * 400)
        with connect() as db:
            db.execute("INSERT INTO recording_holds(id,segment_id,camera_id) VALUES('hold-1',?,?)", (held, self.cameras["north"]))
        result, considered = self.run_pass()
        self.assertEqual((result["removed_segments"], considered), (0, 0))
        self.assertEqual(self.remaining(), [held])

    def test_the_candidates_are_found_through_the_camera_and_time_index(self):
        with connect() as db:
            plan = " | ".join(row[3] for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT s.id FROM recording_segments s WHERE s.camera_id=? AND s.starts_at<? AND s.ends_at<? ORDER BY s.starts_at LIMIT ?", ("c", "a", "a", 10)))
        self.assertIn("recording_camera_time_idx", plan)
        self.assertNotIn("TEMP B-TREE", plan)
