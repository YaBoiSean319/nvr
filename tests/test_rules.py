from __future__ import annotations

import unittest

from app.rules import RulesManager
from tests.fixtures import detection_scene as scene
from tests.scene_case import SceneTestCase, parse

# In the fixture scene the red person walks left to right along the top of the frame,
# the green person right to left just below them, and the blue vehicle right to left
# along the bottom. Each lasts the whole 15-second scene.
MIDDLE_LINE = {"x1": 0.5, "y1": 0.0, "x2": 0.5, "y2": 1.0}  # forward is left to right
TOP_ROW = {"x": 0.0, "y": 0.0, "width": 1.0, "height": 0.55}
BOTTOM_ROW = {"x": 0.0, "y": 0.6, "width": 1.0, "height": 0.4}
BOTTOM_LEFT = {"x": 0.0, "y": 0.6, "width": 0.3, "height": 0.4}


class RulesEngineTests(SceneTestCase):
    def rule(self, condition: dict, **extra) -> dict:
        payload = {"name": "Rule", "enabled": True, "camera_ids": [self.camera_id], "group_ids": [],
                   "condition": condition, "classes": ["person", "vehicle"], "schedule": None, "cooldown_seconds": 0, **extra}
        created = self.client.post("/api/rules", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def run_rules_over_scene(self) -> None:
        self.analyse_footage(companions=[RulesManager(poll_seconds=0.05)])

    def events(self, **params) -> list[dict]:
        response = self.client.get("/api/rule-events", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["events"]

    def colors(self, events: list[dict]) -> list[str]:
        result = []
        for event in events:
            track = self.client.get(f"/api/ai/tracks/{event['track_id']}").json()
            result.append(track["detections"][0]["attributes"]["color"])
        return sorted(result)

    def test_line_crossing_in_one_direction_fires_once_for_the_track_that_crosses(self):
        rule = self.rule({"type": "line_crossing", "line": MIDDLE_LINE, "direction": "forward"}, classes=["person"])
        self.run_rules_over_scene()

        events = self.events(rule_id=rule["id"])
        self.assertEqual(len(events), 1, events)
        self.assertEqual(self.colors(events), ["red"])
        event = events[0]
        self.assertEqual((event["rule_name"], event["condition_type"], event["label"], event["camera_id"]),
                         ("Rule", "line_crossing", "person", self.camera_id))
        # The red person's centre passes x=0.5 between frames 16 and 17.
        self.assertEqual(parse(event["occurred_at"]), scene.frame_times(self.scene_start)[17])

    def test_zone_entry_dwell_and_wrong_way_each_fire_once_for_the_matching_track(self):
        entry = self.rule({"type": "zone_entry", "zone": BOTTOM_LEFT}, name="Gate entry")
        loitering = self.rule({"type": "loitering", "zone": BOTTOM_ROW, "dwell_seconds": 10}, name="Dock dwell")
        person_dwell = self.rule({"type": "loitering", "zone": TOP_ROW, "dwell_seconds": 20}, name="Longer than the scene")
        wrong_way = self.rule({"type": "wrong_way", "zone": TOP_ROW, "direction": "right"}, name="One-way corridor", classes=["person"])
        self.run_rules_over_scene()
        times = scene.frame_times(self.scene_start)

        entered = self.events(rule_id=entry["id"])
        self.assertEqual(self.colors(entered), ["blue"])
        # The vehicle's centre reaches 30% of the frame width at frame 27.
        self.assertEqual(parse(entered[0]["occurred_at"]), times[27])

        dwelled = self.events(rule_id=loitering["id"])
        self.assertEqual(self.colors(dwelled), ["blue"])
        self.assertEqual(parse(dwelled[0]["occurred_at"]), times[20])  # 10 seconds after it was first seen
        self.assertEqual(self.events(rule_id=person_dwell["id"]), [])

        against = self.events(rule_id=wrong_way["id"])
        self.assertEqual(self.colors(against), ["green"])
        self.assertEqual(parse(against[0]["occurred_at"]), times[4])  # 10% of the frame leftwards

    def test_schedules_classes_and_cooldowns_suppress_events(self):
        from datetime import datetime, timezone

        crossing = {"type": "line_crossing", "line": MIDDLE_LINE, "direction": "both"}
        everyone = self.rule(crossing, name="Everyone")
        vehicles = self.rule(crossing, name="Vehicles", classes=["vehicle"])
        # Both people cross the middle line between the same two frames; the cooldown keeps one.
        cooled = self.rule(crossing, name="Cooldown", classes=["person"], cooldown_seconds=3600)
        never_today = (datetime.now(timezone.utc).weekday() + 3) % 7
        off_hours = self.rule(crossing, name="Off hours",
                              schedule={"timezone": "UTC", "intervals": [{"days": [never_today], "start": "00:00", "end": "23:59"}]})
        self.run_rules_over_scene()

        self.assertEqual(self.colors(self.events(rule_id=everyone["id"])), ["blue", "green", "red"])
        self.assertEqual(self.colors(self.events(rule_id=vehicles["id"])), ["blue"])
        self.assertEqual(len(self.events(rule_id=cooled["id"])), 1)
        self.assertEqual(self.events(rule_id=off_hours["id"]), [])

    def test_a_group_rule_watches_policy_and_browsing_members(self):
        group = self.client.post("/api/camera-groups", json={"name": "Dock"})
        self.assertEqual(group.status_code, 201, group.text)
        # The camera is only a browsing member; it keeps no policy group of its own.
        members = self.client.put(f"/api/camera-groups/{group.json()['id']}/members", json={"camera_ids": [self.camera_id]})
        self.assertEqual(members.status_code, 200, members.text)
        rule = self.rule({"type": "zone_entry", "zone": BOTTOM_ROW}, camera_ids=[], group_ids=[group.json()["id"]], classes=["vehicle"])
        self.run_rules_over_scene()
        self.assertEqual(self.colors(self.events(rule_id=rule["id"])), ["blue"])

    def test_deleting_a_rule_keeps_its_event_history(self):
        rule = self.rule({"type": "zone_entry", "zone": BOTTOM_LEFT}, name="Gate entry")
        self.run_rules_over_scene()
        self.assertEqual(self.client.delete(f"/api/rules/{rule['id']}").status_code, 204)
        kept = self.events(camera_id=self.camera_id)
        self.assertEqual([(event["rule_name"], event["rule_id"]) for event in kept], [("Gate entry", None)])

    def test_wrong_way_counts_travel_back_from_the_furthest_point_reached(self):
        from datetime import datetime, timedelta, timezone
        from app.rules import PathPoint, Rule, condition_moments

        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        # Forward (rightwards) to 0.6, then back: the event is where it is 0.1 short of 0.6.
        xs = [0.2, 0.4, 0.6, 0.55, 0.52, 0.49, 0.45]
        path = [PathPoint(index, start + timedelta(seconds=index), x, 0.5, {"x": x, "y": 0.5, "width": 0.01, "height": 0.01}) for index, x in enumerate(xs)]
        rule = Rule("r", "r", True, frozenset(), frozenset(), "wrong_way", {"zone": {"x": 0, "y": 0, "width": 1, "height": 1}, "direction": "right"},
                    frozenset({"person"}), None, timedelta(0), start)
        self.assertEqual([point.detection_id for point in condition_moments(rule, path)], [5])

    def test_test_mode_lists_would_be_events_for_the_last_day_and_creates_nothing(self):
        self.run_rules_over_scene()  # no rules yet: detections are stored, nothing fires
        self.move_scene_into_past()
        draft = {"name": "Draft", "enabled": True, "camera_ids": [self.camera_id], "group_ids": [], "classes": ["person"],
                 "condition": {"type": "line_crossing", "line": MIDDLE_LINE, "direction": "reverse"}, "schedule": None, "cooldown_seconds": 0}

        tested = self.client.post("/api/rules/test", json=draft)
        self.assertEqual(tested.status_code, 200, tested.text)
        body = tested.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(self.colors(body["events"]), ["green"])
        self.assertEqual(body["events"][0]["camera_name"], self.camera_name)
        self.assertEqual(self.client.get("/api/rules").json()["rules"], [])
        self.assertEqual(self.events(), [])

        # A saved rule only fires for what happens after it is saved; test mode still replays the past.
        # Even when the worker had not yet caught up with those detections when the rule was saved.
        from app.rules import process_pending
        from app.store import connect
        with connect() as db:
            db.execute("UPDATE rules_cursor SET last_detection_id=0")
        saved = self.rule(draft["condition"], classes=["person"])
        self.assertGreater(process_pending(), 0)
        self.assertEqual(self.events(rule_id=saved["id"]), [])
        self.assertEqual(self.client.post("/api/rules/test", json=draft).json()["count"], 1)

    def test_editing_or_disabling_a_rule_applies_to_the_running_worker(self):
        rule = self.rule({"type": "zone_entry", "zone": TOP_ROW}, classes=["person"])
        disabled = {**{key: rule[key] for key in ("name", "camera_ids", "group_ids", "condition", "classes", "schedule", "cooldown_seconds")}, "enabled": False}
        manager = RulesManager(poll_seconds=0.05)

        class DisableMidScene:
            """Disables the rule through the API while the worker is running, before any frame."""

            def __init__(self, test):
                self.test = test

            async def start(self):
                updated = self.test.client.put(f"/api/rules/{rule['id']}", json=disabled)
                self.test.assertEqual(updated.status_code, 200, updated.text)

            async def stop(self):
                pass

        self.analyse_footage(companions=[manager, DisableMidScene(self)])
        self.assertEqual(self.events(rule_id=rule["id"]), [])

        enabled = self.client.put(f"/api/rules/{rule['id']}", json={**disabled, "enabled": True, "classes": ["vehicle"],
                                                                     "condition": {"type": "zone_entry", "zone": BOTTOM_ROW}})
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.analyse_footage(companions=[RulesManager(poll_seconds=0.05)])
        self.assertEqual(self.colors(self.events(rule_id=rule["id"])), ["blue"])


if __name__ == "__main__":
    unittest.main()
