"""Occupancy rules (#254): more than a set number of objects in the frame or a zone, for a set time."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import occupancy, rules
from app.store import connect, initialize

START = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def seen(track: str, seconds: range, x: float = 0.5, y: float = 0.5, step: float = 0.5) -> list[tuple[str, datetime, float, float]]:
    return [(track, START + timedelta(seconds=second * step), x, y) for second in range(seconds.start, seconds.stop)]


class ExceedanceTests(unittest.TestCase):
    def moments(self, detections, **condition):
        return [at - START for at in occupancy.exceedances(sorted(detections, key=lambda item: item[1]), condition)]

    def test_fires_once_when_the_count_has_stayed_over_the_limit_for_the_set_time(self):
        crowd = seen("a", range(0, 80)) + seen("b", range(0, 80)) + seen("c", range(0, 80))  # three people for 40 s
        self.assertEqual(self.moments(crowd, limit=2, for_seconds=20), [timedelta(seconds=20)])

    def test_at_the_limit_is_not_over_it(self):
        self.assertEqual(self.moments(seen("a", range(0, 80)) + seen("b", range(0, 80)), limit=2, for_seconds=5), [])

    def test_a_brief_crowd_shorter_than_the_set_time_does_not_fire(self):
        crowd = seen("a", range(0, 80)) + seen("b", range(0, 80)) + seen("c", range(0, 12))  # a third person for 6 s
        self.assertEqual(self.moments(crowd, limit=2, for_seconds=20), [])

    def test_a_second_crowd_after_a_dip_fires_again(self):
        first = seen("a", range(0, 60)) + seen("b", range(0, 60)) + seen("c", range(0, 60))
        second = seen("a", range(200, 260)) + seen("b", range(200, 260)) + seen("c", range(200, 260))
        self.assertEqual(len(self.moments(first + second, limit=2, for_seconds=10)), 2)

    def test_with_a_zone_only_people_inside_it_count(self):
        zone = {"x": 0.6, "y": 0, "width": 0.4, "height": 1}
        inside = seen("a", range(0, 60), x=0.8) + seen("b", range(0, 60), x=0.7)
        outside = seen("c", range(0, 60), x=0.1) + seen("d", range(0, 60), x=0.2)
        self.assertEqual(self.moments(inside + outside, zone=zone, limit=2, for_seconds=5), [])
        self.assertEqual(len(self.moments(inside + outside + seen("e", range(0, 60), x=0.9), zone=zone, limit=2, for_seconds=5)), 1)

    def test_detections_every_second_still_count_as_present_between_them(self):
        crowd = [(name, START + timedelta(seconds=second), 0.5, 0.5) for name in "abc" for second in range(0, 40, 2)]
        self.assertEqual(len(self.moments(crowd, limit=2, for_seconds=10)), 1)


class OccupancyRuleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.directory.name) / "state.sqlite3")
        initialize()
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('cam','Dining room','x')")
            db.execute("INSERT INTO rules(id,name,condition_type,condition_json,classes_json,active_from) VALUES('crowded','Crowded','occupancy','{\"limit\":2,\"for_seconds\":10,\"zone\":null}','[\"person\"]','2026-11-01T00:00:00+00:00')")
            db.execute("UPDATE rules SET camera_ids_json='[\"cam\"]'")
            for name in "abc":
                db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,?,?,?,'m','1','cpu')", (name, "cam", "person", START.isoformat(), (START + timedelta(seconds=40)).isoformat()))
                for second in range(0, 40):
                    db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) VALUES('cam',?,?,'person',0.9,0.4,0.4,0.1,0.2,'m','1','cpu')",
                               (name, (START + timedelta(seconds=second)).isoformat()))

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.directory.cleanup()

    def test_the_worker_raises_one_alarm_for_a_crowd_however_many_passes_see_it(self):
        while rules.process_pending(batch_size=25):
            pass
        rules.process_pending()
        with connect() as db:
            events = db.execute("SELECT label,condition_type,camera_id FROM rule_events").fetchall()
        self.assertEqual([tuple(row) for row in events], [("occupancy", "occupancy", "cam")])

    def test_test_mode_replays_the_same_crowd(self):
        with connect() as db:
            rule = rules.load_rules(db)[0]
        fired = rules.replay(rule, START - timedelta(minutes=1), START + timedelta(minutes=5))
        self.assertEqual([(event["camera_name"], event["label"]) for event in fired], [("Dining room", "occupancy")])

    def test_the_rules_table_still_rejects_unknown_conditions(self):
        with connect() as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO rules(name,condition_type,condition_json,classes_json,active_from) VALUES('Bad','nonsense','{}','[]','2026-11-06T00:00:00+00:00')")


if __name__ == "__main__":
    unittest.main()
