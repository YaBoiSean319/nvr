"""Removing detections costs what is removed, not the square of the track's length (#346)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.store import connect
from tests.test_headroom import HeadroomCase

BASE = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


class TrackRemovedTests(HeadroomCase):
    def track(self, name: str, count: int, *, best_at: int = 0) -> None:
        with connect() as db:
            db.execute("BEGIN")
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,'vehicle',?,?,'m','1','CPU')",
                       (name, self.cameras["north"], BASE.isoformat(), BASE.isoformat()))
            db.executemany(
                "INSERT INTO ai_detections(track_id,camera_id,label,confidence,detected_at,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                "VALUES(?,?,'vehicle',?,?,0.1,0.1,0.2,0.2,'m','1','CPU')",
                ((name, self.cameras["north"], 0.95 if index == best_at else 0.6, (BASE + timedelta(seconds=2 * index)).isoformat()) for index in range(count)))
            db.execute("COMMIT")

    def summary(self, name: str):
        with connect() as db:
            row = db.execute("SELECT first_seen_at,last_seen_at,detection_count,max_confidence FROM ai_tracks WHERE id=?", (name,)).fetchone()
        return None if row is None else tuple(row)

    def test_the_tracks_times_and_count_stay_right_as_its_oldest_detections_go(self):
        self.track("car", 10)
        self.assertEqual(self.summary("car"), (BASE.isoformat(), (BASE + timedelta(seconds=18)).isoformat(), 10, 0.95))
        with connect() as db:
            db.execute("DELETE FROM ai_detections WHERE track_id='car' AND detected_at<?", ((BASE + timedelta(seconds=6)).isoformat(),))
        first, last, count, best = self.summary("car")
        self.assertEqual((first, last, count), ((BASE + timedelta(seconds=6)).isoformat(), (BASE + timedelta(seconds=18)).isoformat(), 7))
        self.assertEqual(best, 0.95, "the best the object was ever seen with is kept")

    def test_a_track_with_no_detections_left_is_removed(self):
        self.track("car", 3)
        with connect() as db:
            db.execute("DELETE FROM ai_detections WHERE track_id='car'")
        self.assertIsNone(self.summary("car"))

    def test_removing_a_minute_of_a_long_track_does_not_read_the_rest_of_it(self):
        self.track("parked", 20000)  # About eleven hours in view at the default rate
        with connect() as db:
            steps = 0

            def count() -> int:
                nonlocal steps
                steps += 1
                return 0

            db.set_progress_handler(count, 1000)
            removed = db.execute("DELETE FROM ai_detections WHERE track_id='parked' AND detected_at<?", ((BASE + timedelta(seconds=60)).isoformat(),)).rowcount
            db.set_progress_handler(None, 0)
        self.assertEqual(removed, 30)
        self.assertLess(steps, 30, "each removed detection re-read the whole track")  # It was about 20,000 rows read for each of the 30
        self.assertEqual(self.summary("parked")[2], 19970)
