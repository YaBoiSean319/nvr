"""The record of when each camera was analysed is read for the range asked, and pruned with the data it describes (#351).

It gained rows for the life of a camera, was never pruned, and every reader scanned all of a camera's rows. Insights also held the whole
path of every track in a range of up to 92 days in memory at once."""
from __future__ import annotations

import random
import tracemalloc
from datetime import datetime, timedelta, timezone

from app import ai_coverage, insights
from app.clock import stamp
from app.retention import cleanup_expired_recordings
from app.store import connect
from tests.test_insights import InsightsCase

T0 = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
OLD_QUERY = ("SELECT camera_id,state,starts_at,ends_at,detail FROM ai_coverage WHERE camera_id=? AND starts_at<? AND (ends_at IS NULL OR ends_at>?) ORDER BY starts_at")


def at(seconds: float) -> str:
    return stamp(T0 + timedelta(seconds=seconds))


class CoverageCase(InsightsCase):
    def setUp(self):
        super().setUp()
        with connect() as db:
            db.execute("DELETE FROM ai_coverage")

    def chain(self, camera_id: str, count: int, seed: int = 1, open_end: bool = True) -> float:
        """A camera's coverage as it is written: each stretch closed when the next begins. Returns when the last one starts."""
        chance, moment, rows = random.Random(seed), 0.0, []
        for index in range(count):
            length = chance.choice((2.0, 2.0, 30.0, 600.0))
            last = index == count - 1
            rows.append((camera_id, chance.choice(("analyzed", "analyzed", "delayed", "skipped", "failed")), at(moment), None if last and open_end else at(moment + length), ""))
            moment += length
        with connect() as db:
            db.executemany("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at,detail) VALUES(?,?,?,?,?)", rows)
        return moment


class ReadingTests(CoverageCase):
    def test_a_range_gets_exactly_the_stretches_that_touch_it(self):
        end_of_chain = self.chain(self.d1, 400)
        self.chain(self.d2, 50, seed=2)  # Another camera's rows are never returned
        chance = random.Random(7)
        with connect() as db:
            for _ in range(200):
                start = chance.uniform(-500, end_of_chain + 500)
                end = start + chance.choice((0.5, 5, 600, 86_400))
                expected = [tuple(row) for row in db.execute(OLD_QUERY, (self.d1, at(end), at(start)))]
                self.assertEqual([tuple(row) for row in ai_coverage.overlapping(db, self.d1, at(start), at(end))], expected, (start, end))

    def test_a_range_that_begins_exactly_where_a_stretch_does(self):
        with connect() as db:
            db.execute("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at) VALUES(?,?,?,?)", (self.d1, "analyzed", at(0), at(10)))
            db.execute("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at) VALUES(?,?,?,?)", (self.d1, "delayed", at(10), at(20)))
            self.assertEqual([row["state"] for row in ai_coverage.overlapping(db, self.d1, at(10), at(15))], ["delayed"])
            self.assertEqual([row["state"] for row in ai_coverage.overlapping(db, self.d1, at(9), at(10))], ["analyzed"])
            self.assertEqual(ai_coverage.overlapping(db, self.d1, at(20), at(30)), [])

    def test_a_stretch_left_open_long_ago_still_covers_a_later_range(self):
        with connect() as db:
            db.execute("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at) VALUES(?,?,?,NULL)", (self.d1, "analyzed", at(0)))  # Never closed: the process died
        self.chain(self.d1, 20, seed=3)
        with connect() as db:
            expected = [tuple(row) for row in db.execute(OLD_QUERY, (self.d1, at(5000), at(4000)))]
            self.assertEqual([tuple(row) for row in ai_coverage.overlapping(db, self.d1, at(4000), at(5000))], expected)
            self.assertIn(at(0), [row["starts_at"] for row in ai_coverage.overlapping(db, self.d1, at(4000), at(5000))])

    def test_reading_ten_minutes_costs_the_same_however_long_the_history(self):
        def cost(rows: int) -> int:
            with connect() as db:
                db.execute("DELETE FROM ai_coverage")
                db.executemany("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at) VALUES(?,'analyzed',?,?)",
                               [(self.d1, at(index * 2), at(index * 2 + 2)) for index in range(rows)])
            steps = [0]
            with connect() as db:
                db.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 10)
                found = ai_coverage.overlapping(db, self.d1, at(rows * 2 - 600), at(rows * 2))
                db.set_progress_handler(None, 0)
            self.assertEqual(len(found), 300)
            return steps[0]

        short, long = cost(1_000), cost(60_000)
        self.assertLess(long, short * 1.5 + 20, f"{long} steps with 60,000 rows of history, {short} with 1,000")


class PruneTests(CoverageCase):
    NOW = T0 + timedelta(days=100)

    def rows(self) -> list[tuple[str, str | None]]:
        with connect() as db:
            return [(row[0], row[1]) for row in db.execute("SELECT starts_at,ends_at FROM ai_coverage WHERE camera_id=? ORDER BY starts_at", (self.d1,))]

    def add(self, start_days: float, end_days: float | None) -> None:
        with connect() as db:
            db.execute("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at) VALUES(?,?,?,?)",
                       (self.d1, "analyzed", at(start_days * 86_400), None if end_days is None else at(end_days * 86_400)))

    def test_what_ended_before_the_retention_period_goes_and_the_rest_stays(self):
        self.add(0, 1)       # Ended 99 days ago
        self.add(1, 69.5)    # Ended 30.5 days ago
        self.add(69.5, 80)   # Ended 20 days ago: inside a 30-day retention
        self.add(80, None)   # Still running
        with connect() as db:
            self.assertEqual(ai_coverage.prune(db, self.d1, 30, self.NOW), 2)
        self.assertEqual([start for start, _ in self.rows()], [at(69.5 * 86_400), at(80 * 86_400)])

    def test_a_stretch_whose_footage_is_still_kept_stays(self):
        self.add(0, 1)
        self.add(1, 2)
        with connect() as db:  # Footage from the first day is still there (held for a case, say)
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,100)",
                       (self.d1, "kept.mp4", at(-60), at(3600)))
            self.assertEqual(ai_coverage.prune(db, self.d1, 30, self.NOW), 1)
        self.assertEqual([start for start, _ in self.rows()], [at(0)])

    def test_years_of_old_rows_go_a_batch_at_a_time(self):
        with connect() as db:
            db.executemany("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at) VALUES(?,'analyzed',?,?)",
                           [(self.d1, at(index * 2), at(index * 2 + 2)) for index in range(ai_coverage.PRUNE_BATCH + 700)])
            self.assertEqual(ai_coverage.prune(db, self.d1, 30, self.NOW), ai_coverage.PRUNE_BATCH)
            self.assertEqual(ai_coverage.prune(db, self.d1, 30, self.NOW), 700)
            self.assertEqual(ai_coverage.prune(db, self.d1, 30, self.NOW), 0)

    def test_a_pass_with_nothing_due_never_writes(self):
        self.add(80, 90)
        with connect() as db:
            before = db.total_changes
            self.assertEqual(ai_coverage.prune(db, self.d1, 30, self.NOW), 0)
            self.assertEqual((db.total_changes, db.in_transaction), (before, False))
            plan = " ".join(row["detail"] for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM ai_coverage WHERE camera_id=? AND ends_at IS NOT NULL AND ends_at<? LIMIT 1", (self.d1, "2020")))
            self.assertIn("ai_coverage_ended_idx", plan)

    def test_the_retention_pass_prunes_it(self):
        self.add(0, 1)
        self.add(99, None)
        cleanup_expired_recordings(self.NOW)
        self.assertEqual([start for start, _ in self.rows()], [at(99 * 86_400)])


class InsightsMemoryTests(CoverageCase):
    def peak(self, points: int) -> int:
        """The most memory an Insights count holds for twenty tracks of that many points each."""
        base = datetime(2026, 1, 10, 8, 0, tzinfo=timezone.utc)
        with connect() as db:
            db.execute("DELETE FROM ai_tracks")
        for index in range(20):
            self.add_track(self.d1, "person", base + timedelta(minutes=index), path=[(0.2 + 0.6 * (step % 2), 0.5) for step in range(points)], seconds=3600, keyframe=False)
        with connect() as db:
            cameras = {self.d1: "Dock 1"}
            tracemalloc.start()
            try:
                result = insights.counts(db, cameras, base, base + timedelta(hours=4), "hour")
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        self.crossings = sum(sum(cell) for cell in result["counts"][f"line:{self.line_rule}:{self.d1}"])
        return peak

    def test_counting_zones_and_lines_does_not_hold_the_paths(self):
        short = self.peak(50)
        self.assertEqual(self.crossings, 20 * 49)  # Every back-and-forth is still counted
        long = self.peak(2_000)
        self.assertEqual(self.crossings, 20 * 1_999)
        # Forty times the points. Holding the paths took memory in proportion (tens of megabytes here); a stream takes about the same.
        self.assertLess(long, short * 3 + 1_000_000, f"{long} bytes at 2,000 points a track, {short} at 50")
