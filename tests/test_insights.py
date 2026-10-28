"""Activity insights (#135): counts per camera, zone and line by hour and day, the heatmap, unanalysed time, the CSV, and access grants."""
from __future__ import annotations

import csv
import io
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from app import insights
from app.store import connect
from tests.test_attribute_search import SearchCase

BASE = datetime(2026, 1, 10, 8, 0, tzinfo=timezone.utc)  # A Saturday morning
RANGE = {"start": BASE.isoformat(), "end": (BASE + timedelta(hours=4)).isoformat()}
LEFT_TO_RIGHT = [(0.2, 0.5), (0.8, 0.5)]  # Crosses the vertical line at x=0.5 and enters the right half
STAYS_LEFT = [(0.1, 0.5), (0.3, 0.5)]


class InsightsCase(SearchCase):
    def setUp(self):
        super().setUp()
        self.zone_rule = self.add_rule("Right half", self.d1, "zone_entry", {"zone": {"x": 0.5, "y": 0, "width": 0.5, "height": 1}}, ["person", "vehicle"])
        self.line_rule = self.add_rule("Gate line", self.d1, "line_crossing", {"line": {"x1": 0.5, "y1": 0, "x2": 0.5, "y2": 1}, "direction": "both"}, ["person"])
        # The AI watched Dock 1 from 08:00 to 09:00 and from 09:30 to 12:00: half of 09:00 was not analysed.
        self.add_coverage(self.d1, BASE, BASE + timedelta(hours=1))
        self.add_coverage(self.d1, BASE + timedelta(hours=1, minutes=30), BASE + timedelta(hours=4))
        self.add_coverage(self.d2, BASE, BASE + timedelta(hours=4))

    def add_rule(self, name: str, camera_id: str, kind: str, condition: dict, classes: list[str]) -> str:
        rule_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO rules(id,name,camera_ids_json,condition_type,condition_json,classes_json,active_from) VALUES(?,?,?,?,?,?,?)",
                       (rule_id, name, json.dumps([camera_id]), kind, json.dumps(condition), json.dumps(classes), (BASE - timedelta(days=30)).isoformat()))
        return rule_id

    def add_coverage(self, camera_id: str, start: datetime, end: datetime, state: str = "analyzed") -> None:
        with connect() as db:
            db.execute("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at) VALUES(?,?,?,?)", (camera_id, state, start.isoformat(), end.isoformat()))

    def fixture_tracks(self):
        self.add_track(self.d1, "person", BASE + timedelta(minutes=10), path=LEFT_TO_RIGHT, keyframe=False)            # 08h: zone 1, line 1
        self.add_track(self.d1, "person", BASE + timedelta(minutes=20), path=STAYS_LEFT, keyframe=False)               # 08h: camera only
        self.add_track(self.d1, "vehicle", BASE + timedelta(minutes=40), path=LEFT_TO_RIGHT, keyframe=False)           # 08h: zone (vehicle), no line (people only)
        self.add_track(self.d1, "person", BASE + timedelta(hours=1, minutes=10), path=[(0.2, 0.5), (0.8, 0.5), (0.2, 0.5)], keyframe=False)  # 09h: zone once, line twice
        self.add_track(self.d1, "person", BASE + timedelta(hours=2, minutes=5), path=LEFT_TO_RIGHT, keyframe=False)    # 10h
        self.add_track(self.d2, "person", BASE + timedelta(minutes=15), path=LEFT_TO_RIGHT, keyframe=False)            # another camera, no zone drawn
        self.add_track(self.d1, "person", BASE - timedelta(hours=1), path=LEFT_TO_RIGHT, keyframe=False)               # before the range

    def get(self, client, path="/api/insights", **extra):
        return client.get(path, params={**RANGE, **extra})

    def series(self, body, subject_id):
        return body["counts"][subject_id]


class CountTests(InsightsCase):
    def test_fixture_tracks_give_the_expected_counts_per_camera_zone_line_and_hour(self):
        self.fixture_tracks()
        body = self.get(self.admin, camera_ids=[self.d1, self.d2]).json()
        self.assertEqual([subject["kind"] for subject in body["subjects"]], ["camera", "camera", "zone", "line"])
        self.assertEqual(len(body["buckets"]), 4)
        # [person, vehicle] per hour 08, 09, 10, 11
        self.assertEqual(self.series(body, f"camera:{self.d1}"), [[2, 1], [1, 0], [1, 0], [0, 0]])
        self.assertEqual(self.series(body, f"camera:{self.d2}"), [[1, 0], [0, 0], [0, 0], [0, 0]])
        self.assertEqual(self.series(body, f"zone:{self.zone_rule}:{self.d1}"), [[1, 1], [1, 0], [1, 0], [0, 0]])  # once per track, however often it re-enters
        self.assertEqual(self.series(body, f"line:{self.line_rule}:{self.d1}"), [[1, 0], [2, 0], [1, 0], [0, 0]])  # each crossing; the rule watches people only

    def test_a_label_filter_and_a_day_unit(self):
        self.fixture_tracks()
        people = self.get(self.admin, camera_ids=[self.d1], label="person").json()
        self.assertEqual(self.series(people, f"camera:{self.d1}"), [[2, 0], [1, 0], [1, 0], [0, 0]])
        day = self.admin.get("/api/insights", params={"start": BASE.isoformat(), "end": (BASE + timedelta(days=2)).isoformat(), "unit": "day", "camera_ids": [self.d1]}).json()
        self.assertEqual(len(day["buckets"]), 3)  # 08:00 on the 10th to 08:00 on the 12th touches three calendar days
        self.assertEqual(self.series(day, f"camera:{self.d1}")[0], [4, 1])

    def test_a_clock_change_makes_a_day_23_hours_and_an_hour_never_stretches(self):
        zone = insights.ZoneInfo("America/New_York")
        spring = insights.buckets(datetime(2026, 3, 7, 12, tzinfo=timezone.utc), datetime(2026, 3, 10, 12, tzinfo=timezone.utc), zone, "day")
        self.assertEqual([(end - begin).total_seconds() / 3600 for begin, end in spring], [24, 23, 24, 24])
        hours = insights.buckets(datetime(2026, 3, 8, 5, 30, tzinfo=timezone.utc), datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc), zone, "hour")
        self.assertTrue(all((end - begin) == timedelta(hours=1) for begin, end in hours))

    def test_the_range_is_validated(self):
        for params in ({"start": "yesterday", "end": RANGE["end"]}, {"start": RANGE["end"], "end": RANGE["start"]}, {"start": BASE.isoformat(), "end": (BASE + timedelta(days=40)).isoformat()},
                       {**RANGE, "label": "dog"}, {**RANGE, "unit": "week"}):
            self.assertEqual(self.admin.get("/api/insights", params=params).status_code, 422, params)
        self.assertEqual(self.admin.get("/api/insights", params={"start": BASE.isoformat(), "end": (BASE + timedelta(days=40)).isoformat(), "unit": "day"}).status_code, 200)


class UnanalysedTests(InsightsCase):
    def test_hours_that_were_not_fully_analysed_are_marked_with_how_much_was(self):
        self.fixture_tracks()
        body = self.get(self.admin, camera_ids=[self.d1]).json()
        self.assertEqual(body["analysed"][self.d1], [100.0, 50.0, 100.0, 100.0])
        self.assertFalse(body["coverage"]["fully_analysed"])
        self.assertEqual(body["coverage"]["cameras"][0]["camera_id"], self.d1)

    def test_a_camera_the_ai_never_watched_is_unanalysed_throughout(self):
        body = self.get(self.admin, camera_ids=[self.y1]).json()
        self.assertEqual(body["analysed"][self.y1], [0.0, 0.0, 0.0, 0.0])

    def test_an_hour_that_has_not_started_is_not_called_unanalysed(self):
        now = BASE + timedelta(hours=1, minutes=30)
        with connect() as db:
            report = insights.counts(db, {self.d2: "Dock 2"}, BASE, BASE + timedelta(hours=4), "hour", now=now)
        self.assertEqual(report["analysed"][self.d2], [100.0, 100.0, None, None])

    def test_the_csv_carries_every_hour_with_the_unanalysed_ones_noted(self):
        self.fixture_tracks()
        response = self.get(self.admin, "/api/insights/export.csv", camera_ids=[self.d1])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/csv"))
        rows = list(csv.DictReader(io.StringIO(response.text)))
        camera = [row for row in rows if row["kind"] == "camera"]
        self.assertEqual([(row["person"], row["vehicle"], row["analysed_percent"]) for row in camera], [("2", "1", "100.0"), ("1", "0", "50.0"), ("1", "0", "100.0"), ("0", "0", "100.0")])
        self.assertEqual([bool(row["note"]) for row in camera], [False, True, False, False])
        self.assertIn("Not fully analysed", camera[1]["note"])
        self.assertEqual({row["kind"] for row in rows}, {"camera", "zone", "line"})
        self.assertEqual(len(rows), 12)  # 4 hours for each of the camera, the zone and the line

    def test_a_name_that_would_run_as_a_formula_is_defused_in_the_csv(self):
        with connect() as db:
            db.execute("UPDATE cameras SET name='=HYPERLINK(1)' WHERE id=?", (self.d1,))
        rows = list(csv.DictReader(io.StringIO(self.get(self.admin, "/api/insights/export.csv", camera_ids=[self.d1]).text)))
        self.assertTrue(all(row["camera"].startswith("'=") for row in rows))


class HeatmapTests(InsightsCase):
    def test_the_heatmap_concentrates_on_the_fixture_paths(self):
        for index in range(6):
            self.add_track(self.d1, "person", BASE + timedelta(minutes=index), path=[(0.1, 0.8), (0.1, 0.8), (0.1, 0.8)], keyframe=False)  # a worn path, bottom left
        self.add_track(self.d1, "person", BASE + timedelta(minutes=30), path=[(0.9, 0.1)], keyframe=False)  # one visit to the top right
        body = self.admin.get("/api/insights/heatmap", params={**RANGE, "camera_id": self.d1}).json()
        self.assertEqual((body["columns"], body["rows"]), (32, 18))
        self.assertEqual(body["tracks"], 7)
        cells = body["cells"]
        hot = cells[int(0.8 * 18)][int(0.1 * 32)]
        cold = cells[int(0.1 * 18)][int(0.9 * 32)]
        self.assertEqual((hot, cold, body["peak"]), (18, 1, 18))
        self.assertEqual(sum(map(sum, cells)), 19)  # nothing anywhere else
        vehicles = self.admin.get("/api/insights/heatmap", params={**RANGE, "camera_id": self.d1, "label": "vehicle"}).json()
        self.assertEqual((vehicles["tracks"], vehicles["peak"]), (0, 0))

    def test_a_centre_on_the_edge_of_the_frame_lands_in_the_last_cell(self):
        self.add_track(self.d1, "person", BASE + timedelta(minutes=1), path=[(1.0, 1.0)], keyframe=False)
        body = self.admin.get("/api/insights/heatmap", params={**RANGE, "camera_id": self.d1}).json()
        self.assertEqual(body["cells"][17][31], 1)

    def test_the_heatmap_says_what_was_not_analysed(self):
        body = self.admin.get("/api/insights/heatmap", params={**RANGE, "camera_id": self.y1}).json()
        self.assertFalse(body["coverage"]["fully_analysed"])


class ReferenceTests(InsightsCase):
    def test_without_a_recording_there_is_nothing_to_draw_over(self):
        self.assertEqual(self.admin.get("/api/insights/reference", params={"camera_id": self.d1}).status_code, 404)
        self.assertEqual(self.admin.get("/api/insights/reference", params={"camera_id": self.d1, "at": "soon"}).status_code, 422)


class AccessTests(InsightsCase):
    def test_a_person_sees_counts_only_for_cameras_they_may_see(self):
        self.fixture_tracks()
        self.add_track(self.y1, "person", BASE + timedelta(minutes=5), path=LEFT_TO_RIGHT, keyframe=False)
        everything = self.get(self.admin).json()
        self.assertEqual({subject["camera_id"] for subject in everything["subjects"]}, {self.d1, self.d2, self.y1, self.loose})
        limited = self.get(self.limited).json()
        self.assertEqual({subject["camera_id"] for subject in limited["subjects"]}, {self.d1, self.d2})
        asked = self.get(self.limited, camera_ids=[self.y1, self.d1]).json()
        self.assertEqual({subject["camera_id"] for subject in asked["subjects"]}, {self.d1})  # a hidden camera answers like one that does not exist
        self.assertNotIn(self.y1, json.dumps(self.get(self.limited, "/api/insights/export.csv", camera_ids=[self.y1]).text))
        self.assertEqual(self.get(self.limited, camera_ids=[self.y1]).json()["subjects"], [])

    def test_the_heatmap_and_the_reference_frame_are_hidden_like_a_missing_camera(self):
        self.add_track(self.y1, "person", BASE + timedelta(minutes=5), path=LEFT_TO_RIGHT, keyframe=False)
        for path in ("/api/insights/heatmap", "/api/insights/reference"):
            hidden = self.limited.get(path, params={**RANGE, "camera_id": self.y1})
            missing = self.limited.get(path, params={**RANGE, "camera_id": str(uuid.uuid4())})
            self.assertEqual((hidden.status_code, hidden.json()), (missing.status_code, missing.json()), path)
            self.assertEqual(hidden.status_code, 404)
        self.assertEqual(self.limited.get("/api/insights/heatmap", params={**RANGE, "camera_id": self.d1}).status_code, 200)

    def test_it_needs_signing_in(self):
        from fastapi.testclient import TestClient
        self.assertEqual(TestClient(self.app).get("/api/insights", params=RANGE).status_code, 401)


if __name__ == "__main__":
    unittest.main()
