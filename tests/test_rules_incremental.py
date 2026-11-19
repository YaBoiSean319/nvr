"""The live rules pass reads only the new part of an object's path, and finds exactly what reading all of it would (#347).

It used to load every watched object's whole path for every new frame: 107 ms a pass for twenty vehicles an hour in view, 2.15 s after a
day. These pin both halves: the same moments as the whole path gives, however the path arrives, and a cost that does not grow with it."""
from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import rules
from app.clock import stamp
from app.store import connect
from tests.test_access_grants import GrantCase

T0 = datetime(2026, 11, 1, 8, 0, 0, tzinfo=timezone.utc)
ZONE = {"x": 0.3, "y": 0.3, "width": 0.4, "height": 0.4}
CONDITIONS = [
    {"type": "zone_entry", "zone": ZONE},
    {"type": "line_crossing", "line": {"x1": 0.5, "y1": 0.0, "x2": 0.5, "y2": 1.0}, "direction": "both"},
    {"type": "line_crossing", "line": {"x1": 0.5, "y1": 0.0, "x2": 0.5, "y2": 1.0}, "direction": "forward"},
    {"type": "loitering", "zone": ZONE, "dwell_seconds": 20},
    {"type": "wrong_way", "zone": ZONE, "direction": "right"},
    {"type": "inactivity", "zone": None, "still_seconds": 15},
    {"type": "inactivity", "zone": ZONE, "still_seconds": 10},
]


class IncrementalCase(GrantCase):
    def setUp(self):
        super().setUp()
        rules._followers.clear()
        self.addCleanup(rules._followers.clear)

    def add_rule(self, condition: dict, cooldown: int = 0, active_from: datetime = T0 - timedelta(days=1)) -> str:
        rule_id = str(uuid.uuid4())
        body = {key: value for key, value in condition.items() if key != "type"}
        with connect() as db:
            db.execute("INSERT INTO rules(id,name,camera_ids_json,condition_type,condition_json,classes_json,cooldown_seconds,active_from) VALUES(?,?,?,?,?,?,?,?)",
                       (rule_id, condition["type"], json.dumps([self.d1]), condition["type"], json.dumps(body), '["vehicle"]', cooldown, active_from.isoformat()))
        return rule_id

    def add_track(self) -> str:
        track_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,'vehicle',?,?,'m','1','CPU')",
                       (track_id, self.d1, stamp(T0), stamp(T0)))
        return track_id

    def add_points(self, track_id: str, points: list[tuple[float, float, float]]) -> None:
        """Store points as (seconds after T0, centre x, centre y)."""
        with connect() as db:
            db.executemany("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                           "VALUES(?,?,?,'vehicle',0.9,?,?,0.02,0.02,'m','1','CPU')",
                           [(self.d1, track_id, stamp(T0 + timedelta(seconds=at)), min(max(x - 0.01, 0.0), 0.98), min(max(y - 0.01, 0.0), 0.98)) for at, x, y in points])

    def found(self, track_id: str) -> dict[str, list[int]]:
        """What the live pass finds for the track now, by rule: the detection ids of its eligible moments."""
        with connect() as db:
            track = db.execute("SELECT id,camera_id,label FROM ai_tracks WHERE id=?", (track_id,)).fetchone()
            loaded = rules.load_rules(db)
            return {rule.id: [point.detection_id for point in moments] for rule, moments in rules._track_moments(db, "test", track, loaded)}

    def whole_path(self, track_id: str) -> dict[str, list[int]]:
        """What reading the whole path finds: how the pass worked before."""
        with connect() as db:
            path = rules._track_path(db, track_id)
            answer = {rule.id: [point.detection_id for point in rules.eligible_moments(rule, path, since=rule.active_from)] for rule in rules.load_rules(db)}
        return {rule_id: moments for rule_id, moments in answer.items() if moments}


def wander(seed: int, count: int, start: float = 0.0) -> list[tuple[float, float, float]]:
    """An object that drifts about, stops for a while, and moves on: in and out of the zone and across the line many times."""
    chance = random.Random(seed)
    x, y, points, at = chance.random(), chance.random(), [], start
    while len(points) < count:
        still = chance.random() < 0.3
        for _ in range(chance.randint(3, 25)):
            at += chance.choice((1.0, 2.0, 2.0, 5.0))
            if not still:
                x = min(max(x + chance.uniform(-0.12, 0.12), 0.02), 0.98)
                y = min(max(y + chance.uniform(-0.12, 0.12), 0.02), 0.98)
            points.append((at, x + chance.uniform(-0.002, 0.002), y))
    return points[:count]


class SameAnswerTests(IncrementalCase):
    def test_every_condition_finds_the_same_moments_however_the_path_arrives(self):
        for condition in CONDITIONS:
            self.add_rule(condition)
        for seed in range(6):
            track_id, points, chance = self.add_track(), wander(seed, 400), random.Random(seed)
            position = 0
            while position < len(points):
                size = chance.choice((1, 1, 2, 7, 40, 150))
                self.add_points(track_id, points[position:position + size])
                position += size
                self.assertEqual(self.found(track_id), self.whole_path(track_id), f"seed {seed}, after {position} points")
            self.assertGreaterEqual(len(self.whole_path(track_id)), 4, "the path should exercise most conditions")

    def test_a_restart_starts_again_from_the_whole_path(self):
        self.add_rule(CONDITIONS[3])
        track_id, points = self.add_track(), wander(3, 300)
        self.add_points(track_id, points[:200])
        self.found(track_id)
        rules._followers.clear()  # The rules worker restarted
        self.add_points(track_id, points[200:])
        self.assertEqual(self.found(track_id), self.whole_path(track_id))

    def test_a_point_stored_late_with_an_earlier_time_is_not_missed(self):
        self.add_rule(CONDITIONS[0])
        track_id = self.add_track()
        self.add_points(track_id, [(0, 0.1, 0.1), (10, 0.1, 0.1)])
        self.assertEqual(self.found(track_id), {})
        self.add_points(track_id, [(5, 0.5, 0.5)])  # Inside the zone, between the two points already read
        self.assertEqual(self.found(track_id), self.whole_path(track_id))
        self.assertEqual(len(next(iter(self.found(track_id).values()))), 1)

    def test_removed_points_are_noticed(self):
        self.add_rule(CONDITIONS[0])
        track_id = self.add_track()
        self.add_points(track_id, [(0, 0.1, 0.1), (2, 0.5, 0.5), (4, 0.1, 0.1)])
        self.assertEqual(len(next(iter(self.found(track_id).values()))), 1)
        with connect() as db:
            db.execute("DELETE FROM ai_detections WHERE track_id=? AND box_x>0.4", (track_id,))
        self.add_points(track_id, [(6, 0.1, 0.1)])
        self.assertEqual(self.found(track_id), self.whole_path(track_id))
        self.assertEqual(self.found(track_id), {})

    def test_an_edited_rule_is_read_against_the_whole_path_again(self):
        rule_id = self.add_rule({"type": "loitering", "zone": ZONE, "dwell_seconds": 3600})
        track_id = self.add_track()
        self.add_points(track_id, [(second, 0.5, 0.5) for second in range(0, 60, 2)])
        self.assertEqual(self.found(track_id), {})
        with connect() as db:
            db.execute("UPDATE rules SET condition_json=? WHERE id=?", (json.dumps({"zone": ZONE, "dwell_seconds": 20}), rule_id))
        self.add_points(track_id, [(60, 0.5, 0.5)])
        self.assertEqual(self.found(track_id), self.whole_path(track_id))
        self.assertEqual(len(self.found(track_id)[rule_id]), 1)

    def test_a_rule_that_cannot_be_read_does_not_stop_the_others(self):
        good = self.add_rule(CONDITIONS[0])
        with connect() as db:
            db.execute("INSERT INTO rules(id,name,camera_ids_json,condition_type,condition_json,classes_json,active_from) VALUES('broken','b',?,'loitering','{}',?,?)",
                       (json.dumps([self.d1]), json.dumps(["vehicle"]), (T0 - timedelta(days=1)).isoformat()))
        track_id = self.add_track()
        self.add_points(track_id, [(0, 0.1, 0.1), (2, 0.5, 0.5)])
        with self.assertLogs("nvr.rules", level="ERROR"):
            self.assertEqual(list(self.found(track_id)), [good])


class LivePassTests(IncrementalCase):
    def events(self) -> list[tuple[str, str]]:
        with connect() as db:
            return [(row["condition_type"], row["occurred_at"]) for row in db.execute("SELECT condition_type,occurred_at FROM rule_events ORDER BY occurred_at,condition_type")]

    def test_passes_frame_by_frame_raise_the_alarms_one_pass_over_everything_would(self):
        for condition in CONDITIONS:
            self.add_rule(condition)
        tracks = [(self.add_track(), wander(seed, 250)) for seed in (11, 12)]
        for index in range(250):
            for track_id, points in tracks:
                self.add_points(track_id, [points[index]])
            if index % 3 == 0:
                rules.process_pending()
        rules.process_pending()
        frame_by_frame = self.events()
        self.assertGreaterEqual(len(frame_by_frame), 6)
        with connect() as db:
            db.execute("DELETE FROM alarms")
            db.execute("DELETE FROM rule_events")
            db.execute("UPDATE rules_cursor SET last_detection_id=0")
        rules._followers.clear()
        rules.process_pending()
        self.assertEqual(self.events(), frame_by_frame)

    def test_a_pass_over_an_object_long_in_view_costs_about_what_one_over_a_new_object_does(self):
        self.add_rule({"type": "loitering", "zone": ZONE, "dwell_seconds": 10 ** 6})  # Watching, and never satisfied
        self.add_rule({"type": "inactivity", "zone": None, "still_seconds": 10 ** 6})

        def cost_of_a_pass(track_id: str, at: float) -> int:
            self.add_points(track_id, [(at, 0.5, 0.5)])
            steps, real = [0], rules.connect

            def counted():
                db = real()
                db.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 20)
                return db

            with mock.patch.object(rules, "connect", counted):
                self.assertEqual(rules.process_pending(), 1)
            return steps[0]

        new = self.add_track()
        self.add_points(new, [(second, 0.5, 0.5) for second in range(0, 20, 2)])
        rules.process_pending()
        short = cost_of_a_pass(new, 20)

        old = self.add_track()
        self.add_points(old, [(second, 0.5, 0.5) for second in range(0, 40_000, 2)])  # Eleven hours parked
        while rules.process_pending():
            pass
        long = cost_of_a_pass(old, 40_000)
        self.assertLess(long, short * 2 + 50, f"{long} steps for the long path, {short} for the short one")
