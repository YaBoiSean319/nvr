"""Event digests (#136): complete and linked, deterministic, scheduled at shift end and delivered through actions, limited to what the reader may see."""
from __future__ import annotations

import asyncio
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app import digests, rule_actions
from app.store import connect
from tests.test_access_grants import GrantCase

DAY = datetime(2026, 1, 10, 0, 0, tzinfo=timezone.utc)  # A Saturday
RANGE = {"start": DAY.isoformat(), "end": (DAY + timedelta(hours=12)).isoformat()}
WEBHOOK = {"type": "webhook", "name": "Control room", "url": "https://8.8.8.8/hook", "signing_secret": "s" * 24}
SLACK = {"type": "slack", "name": "Shift channel", "url": "https://hooks.slack.com/services/T000/B000/XXXX"}


def at(hours: float) -> datetime:
    return DAY + timedelta(hours=hours)


class DigestCase(GrantCase):
    def setUp(self):
        super().setUp()
        with connect() as db:
            db.execute("UPDATE cameras SET created_at='2025-01-01T00:00:00+00:00'")
        checked = rule_actions.validate_destination
        allow = patch("app.rule_actions.validate_destination", side_effect=lambda url, allow_private: url if "hooks.slack.com" in url else checked(url, allow_private))  # Slack's host cannot be resolved here
        allow.start()
        self.addCleanup(allow.stop)
        self.rule = self.add_rule("Gate", self.d1, "zone_entry", ["person", "vehicle"])
        self.line = self.add_rule("Dock line", self.d1, "line_crossing", ["person"])

    def add_rule(self, name: str, camera_id: str, kind: str, classes: list[str]) -> str:
        rule_id = str(uuid.uuid4())
        condition = {"zone": {"x": 0, "y": 0, "width": 1, "height": 1}} if kind == "zone_entry" else {"line": {"x1": 0.5, "y1": 0, "x2": 0.5, "y2": 1}, "direction": "both"}
        with connect() as db:
            db.execute("INSERT INTO rules(id,name,camera_ids_json,condition_type,condition_json,classes_json,active_from) VALUES(?,?,?,?,?,?,?)",
                       (rule_id, name, json.dumps([camera_id]), kind, json.dumps(condition), json.dumps(classes), (DAY - timedelta(days=30)).isoformat()))
        return rule_id

    def add_alarm(self, camera_id: str, when: datetime, *, rule_id: str | None = None, rule_name: str = "Gate", label: str = "person", kind: str = "zone_entry", state: str = "new") -> str:
        event, alarm = str(uuid.uuid4()), str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json) VALUES(?,?,?,?,?,?,?,?,?)",
                       (event, rule_id or self.rule, rule_name, camera_id, f"track-{event}", label, kind, when.isoformat(), '{"x":0,"y":0,"width":0.1,"height":0.1}'))
            db.execute("INSERT INTO alarms(id,rule_event_id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json,clip_starts_at,clip_ends_at,state,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (alarm, event, rule_id or self.rule, rule_name, camera_id, "t", label, kind, when.isoformat(), '{"x":0,"y":0,"width":0.1,"height":0.1}',
                                                                  (when - timedelta(seconds=10)).isoformat(), (when + timedelta(seconds=20)).isoformat(), state, when.isoformat(), when.isoformat()))
        return alarm

    def add_person(self, camera_id: str, when: datetime) -> str:
        track = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,'person',?,?,'m','1','CPU')",
                       (track, camera_id, when.isoformat(), when.isoformat()))
        return track

    def add_segment(self, camera_id: str, start: datetime, end: datetime) -> None:
        with connect() as db:
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,100)",
                       (camera_id, f"{camera_id}/{start.timestamp()}.mp4", start.isoformat(), end.isoformat()))

    def add_incident(self, camera_id: str | None, when: datetime, summary: str) -> str:
        incident = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO incidents(id,incident_key,camera_id,fault_type,summary,state,fault_active,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,'recovered',0,?,?)",
                       (incident, f"key-{incident}", camera_id, "stream_down", summary, when.isoformat(), (when + timedelta(minutes=5)).isoformat()))
        return incident

    def fixture(self):
        """Alarms (three rules and outcomes, one on a hidden camera), people after hours, a vehicle at a gate, a recording gap and two incidents."""
        self.alarms = {
            "resolved": self.add_alarm(self.d1, at(3), state="resolved"), "new": self.add_alarm(self.d1, at(4)),
            "line": self.add_alarm(self.d2, at(5), rule_id=self.line, rule_name="Dock line", kind="line_crossing", state="acknowledged"),
            "gate": self.add_alarm(self.d1, at(6), label="vehicle"), "hidden": self.add_alarm(self.y1, at(7), rule_name="Yard"),
        }
        self.night = self.add_person(self.d1, at(3.5))
        self.add_person(self.d1, at(10))  # By day: not notable
        self.add_person(self.y1, at(2))   # After hours, on a camera the limited operator cannot see
        self.add_segment(self.d1, at(0), at(8))
        self.add_segment(self.d1, at(9), at(16))  # A gap from 08:00 to 09:00
        self.add_segment(self.d2, at(0), at(16))
        self.add_segment(self.y1, at(0), at(16))
        self.add_segment(self.loose, at(0), at(16))
        self.camera_incident = self.add_incident(self.d1, at(8.5), "Dock 1 stream down")
        self.system_incident = self.add_incident(None, at(9.5), "Recording disk nearly full")

    def shift_alarms(self):
        """Two alarms inside the 08:00 to 16:00 shift: one on Dock 1 and one on Yard 1."""
        self.in_shift = {"dock": self.add_alarm(self.d1, at(10)), "yard": self.add_alarm(self.y1, at(11), rule_name="Yard")}

    def preview(self, client, **extra):
        response = client.get("/api/digests/preview", params={**RANGE, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class ContentTests(DigestCase):
    def test_a_digest_lists_every_alarm_gap_and_incident_in_range_with_working_links(self):
        self.fixture()
        body = self.preview(self.admin)["digest"]
        self.assertEqual(body["alarms"]["total"], 5)
        self.assertEqual({item["id"] for item in body["alarms"]["items"]}, set(self.alarms.values()))
        rules = {item["rule_name"]: item for item in body["alarms"]["by_rule"]}
        self.assertEqual(rules["Gate"]["outcomes"], {"new": 2, "acknowledged": 0, "assigned": 0, "resolved": 1})
        self.assertEqual(rules["Dock line"]["outcomes"]["acknowledged"], 1)
        self.assertEqual(body["alarms"]["unresolved"], 4)
        self.assertEqual([item["track_id"] for item in body["tracks"]["after_hours_people"]["items"] if item["camera_id"] == self.d1], [self.night])
        self.assertEqual(body["tracks"]["after_hours_people"]["total"], 2)  # Dock 1 at 03:30 and Yard 1 at 02:00; the 10:00 walker is by day
        [gate] = body["tracks"]["vehicles_at_gates"]["items"]
        self.assertEqual((gate["camera_id"], gate["link"]), (self.d1, f"/?alarm={self.alarms['gate']}"))
        self.assertEqual(body["gaps"]["total_seconds"], 3600.0)
        [gap] = [item for item in body["gaps"]["items"] if item["camera_id"] == self.d1]
        self.assertEqual((gap["starts_at"], gap["ends_at"]), (at(8).isoformat(), at(9).isoformat()))
        self.assertEqual({item["id"] for item in body["incidents"]["items"]}, {self.camera_incident, self.system_incident})
        # Every link opens something that exists.
        for item in body["alarms"]["items"]:
            self.assertEqual(self.admin.get(f"/api/alarms/{item['id']}").status_code, 200)
            self.assertEqual(item["link"], f"/?alarm={item['id']}")
        self.assertEqual(gap["link"], f"/playback?playback={self.d1}&at={gap['starts_at'].replace('+', '%2B').replace(':', '%3A')}")
        self.assertTrue(all(item["link"].startswith("/?incident=") for item in body["incidents"]["items"]))

    def test_the_text_carries_the_same_findings_and_the_same_words_every_time(self):
        self.fixture()
        first, second = self.preview(self.admin), self.preview(self.admin)
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(first["text"], second["text"])
        text = first["text"]
        for alarm in self.alarms.values():
            self.assertIn(f"/?alarm={alarm}", text)
        self.assertIn("Dock 1 stream down", text)
        self.assertIn("Recording disk nearly full", text)
        self.assertIn("5 alarms (4 not resolved)", first["headline"])

    def test_a_scope_narrows_it_to_a_group_or_cameras(self):
        self.fixture()
        dock = self.preview(self.admin, group_ids=[self.dock])
        self.assertEqual({camera["id"] for camera in dock["digest"]["cameras"]}, {self.d1, self.d2})
        self.assertEqual(dock["digest"]["alarms"]["total"], 4)  # Not the Yard alarm
        one = self.preview(self.admin, camera_ids=[self.d2])
        self.assertEqual({item["id"] for item in one["digest"]["alarms"]["items"]}, {self.alarms["line"]})

    def test_time_the_ai_did_not_analyse_is_said_so_a_quiet_digest_is_not_misread(self):
        body = self.preview(self.admin)["digest"]
        self.assertFalse(body["analysis"]["fully_analysed"])
        self.assertIn("Not all of this time was analysed", self.preview(self.admin)["text"])

    def test_the_range_and_times_are_validated(self):
        for params in ({"start": "x", "end": RANGE["end"]}, {"start": RANGE["end"], "end": RANGE["start"]}, {"start": DAY.isoformat(), "end": (DAY + timedelta(days=40)).isoformat()},
                       {**RANGE, "after_hours_start": "25:00"}, {**RANGE, "group_ids": [str(uuid.uuid4())]}):
            self.assertEqual(self.admin.get("/api/digests/preview", params=params).status_code, 422, params)

    def test_after_hours_follow_the_window_given(self):
        self.fixture()
        by_day = self.preview(self.admin, after_hours_start="09:00", after_hours_end="12:00")["digest"]
        self.assertEqual([item["camera_id"] for item in by_day["tracks"]["after_hours_people"]["items"]], [self.d1])


class AccessTests(DigestCase):
    def test_a_digest_includes_only_cameras_the_reader_may_see(self):
        self.fixture()
        body = self.preview(self.limited)["digest"]
        self.assertEqual({camera["id"] for camera in body["cameras"]}, {self.d1, self.d2})
        self.assertNotIn(self.alarms["hidden"], {item["id"] for item in body["alarms"]["items"]})
        self.assertEqual(body["alarms"]["total"], 4)
        self.assertEqual({item["camera_id"] for item in body["tracks"]["after_hours_people"]["items"]}, {self.d1})
        self.assertEqual({item["id"] for item in body["incidents"]["items"]}, {self.camera_incident})  # The disk incident names no camera: for those who see everything
        asked = self.preview(self.limited, camera_ids=[self.y1, self.d1])
        self.assertEqual({camera["id"] for camera in asked["digest"]["cameras"]}, {self.d1})
        self.assertNotIn("Yard", asked["text"])

    def test_a_scheduled_digest_is_made_for_its_recipients_view_and_only_they_and_administrators_can_read_it(self):
        self.fixture()
        self.shift_alarms()
        schedule = self.schedule(self.limited_id, actions=[])
        made = digests.generate_due(now=at(17))
        self.assertEqual(len(made), 1)
        digest = self.limited.get(f"/api/digests/{made[0]}").json()
        ids = {item["id"] for item in digest["digest"]["alarms"]["items"]}
        self.assertEqual(ids, {self.in_shift["dock"]})  # Not the Yard alarm
        self.assertEqual(self.full.get(f"/api/digests/{made[0]}").status_code, 404)  # Made for someone else
        self.assertEqual(self.admin.get(f"/api/digests/{made[0]}").status_code, 200)
        self.assertEqual([item["id"] for item in self.limited.get("/api/digests").json()["digests"]], made)
        self.assertEqual(self.full.get("/api/digests").json()["digests"], [])
        self.assertEqual(schedule["recipient_user_id"], self.limited_id)

    def test_access_taken_away_after_a_digest_was_made_is_respected_when_it_is_read(self):
        self.fixture()
        everything = digests.build(connect().__enter__(), [self.d1, self.y1], at(0), at(12), unrestricted=True)
        narrowed = digests.restrict(everything, {self.d1}, False)
        self.assertEqual({item["camera_id"] for item in narrowed["alarms"]["items"]}, {self.d1})
        self.assertEqual(narrowed["alarms"]["total"], 3)
        self.assertEqual([camera["id"] for camera in narrowed["cameras"]], [self.d1])
        self.assertEqual(narrowed["incidents"]["total"], 1)

    def test_only_administrators_manage_schedules(self):
        for method, path in (("get", "/api/digests/schedules"), ("get", "/api/digests/settings")):
            self.assertEqual(getattr(self.limited, method)(path).status_code, 403, path)
        self.assertEqual(self.limited.post("/api/digests/schedules", json={}).status_code, 403)


class ReviewTests(DigestCase):
    def test_a_restricted_reader_cannot_tell_a_hidden_group_or_camera_from_one_that_does_not_exist(self):
        hidden = self.limited.get("/api/digests/preview", params={**RANGE, "camera_ids": [self.y1]})
        unknown = self.limited.get("/api/digests/preview", params={**RANGE, "camera_ids": [str(uuid.uuid4())]})
        self.assertEqual((hidden.status_code, hidden.json()["digest"]["cameras"]), (unknown.status_code, unknown.json()["digest"]["cameras"]))
        self.assertEqual(hidden.json()["digest"]["cameras"], [])
        self.assertEqual(self.limited.get("/api/digests/preview", params={**RANGE, "group_ids": [self.yard]}).json()["digest"]["cameras"], [])

    def test_system_wide_incidents_belong_to_the_whole_site_not_to_a_digest_of_one_camera(self):
        self.fixture()
        self.assertEqual({item["id"] for item in self.preview(self.admin)["digest"]["incidents"]["items"]}, {self.camera_incident, self.system_incident})
        self.assertEqual({item["id"] for item in self.preview(self.admin, camera_ids=[self.d1])["digest"]["incidents"]["items"]}, {self.camera_incident})

    def test_a_summary_is_not_shown_to_a_reader_whose_view_is_narrower_than_it_was_written_from(self):
        self.fixture()
        self.shift_alarms()
        with connect() as db:
            made = digests.generate(db, self.plain_schedule(self.limited_id), at(8), at(16), now=at(17))
            db.execute("UPDATE digests SET summary='Two alarms, one at the yard.'")
        self.assertEqual(self.limited.get(f"/api/digests/{made}").json()["summary"], "Two alarms, one at the yard.")  # Their view is what it was
        self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.dock}])  # unchanged: still Dock only
        with connect() as db:
            db.execute("UPDATE digests SET body_json=? WHERE id=?", (json.dumps(self.with_extra_camera_alarm(made)), made))  # As if the stored digest had covered more than they may see now
        narrowed = self.limited.get(f"/api/digests/{made}").json()
        self.assertIsNone(narrowed["summary"])
        self.assertNotIn("one at the yard", narrowed["text"])

    def with_extra_camera_alarm(self, digest_id: str) -> dict:
        with connect() as db:
            body = json.loads(db.execute("SELECT body_json FROM digests WHERE id=?", (digest_id,)).fetchone()[0])
        body["alarms"]["items"].append({"id": "x", "rule_name": "Yard", "camera_id": self.y1, "camera_name": "Yard 1", "label": "person", "state": "new", "occurred_at": at(11).isoformat(), "link": "/?alarm=x"})
        body["alarms"]["total"] += 1
        return body

    def plain_schedule(self, recipient: int):
        schedule_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO digest_schedules(id,name,scope_json,recipient_user_id,shift_json,after_hours_json,last_period_end) VALUES(?,?,?,?,?,?,?)",
                       (schedule_id, "Plain", json.dumps({"site": True}), recipient, json.dumps({"days": [0, 1, 2, 3, 4, 5, 6], "start": "08:00", "end": "16:00"}),
                        json.dumps({"start": "22:00", "end": "06:00"}), at(-8).isoformat()))
            return db.execute("SELECT * FROM digest_schedules WHERE id=?", (schedule_id,)).fetchone()

    def test_a_shift_across_a_clock_change_starts_where_the_clock_says(self):
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("America/New_York")  # Clocks go forward at 02:00 on 8 March 2026
        start, end = digests.latest_shift({"days": [6], "start": "22:00", "end": "06:00"}, zone, datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(start.astimezone(zone).strftime("%a %H:%M"), "Sat 22:00")
        self.assertEqual(end.astimezone(zone).strftime("%a %H:%M"), "Sun 06:00")

    def test_a_schedule_that_cannot_be_made_is_not_retried_on_every_pass(self):
        self.fixture()
        created = self.admin.post("/api/digests/schedules", json={"name": "Broken", "scope": {"site": True}, "recipient_user_id": self.full_id,
                                                                   "shift": {"days": [0, 1, 2, 3, 4, 5, 6], "start": "08:00", "end": "16:00"}, "actions": []}).json()
        with connect() as db:
            db.execute("UPDATE digest_schedules SET last_period_end=? WHERE id=?", (at(-8).isoformat(), created["id"]))
        digests._failures.clear()
        self.addCleanup(digests._failures.clear)
        with patch("app.digests.compose", side_effect=RuntimeError("boom")) as compose:
            digests.generate_due(now=at(17))
            digests.generate_due(now=at(17) + timedelta(seconds=20))
            self.assertEqual(compose.call_count, 1)  # The second pass waited out the backoff
            digests.generate_due(now=at(17) + timedelta(minutes=2))
            self.assertEqual(compose.call_count, 2)
        self.assertEqual(len(digests.generate_due(now=at(17) + timedelta(hours=2))), 1)  # And it is made once it can be


class ScheduleTests(DigestCase):
    def schedule(self, recipient: int, *, actions=None, **extra):
        body = {"name": "Day shift", "scope": {"site": True}, "recipient_user_id": recipient, "shift": {"days": [0, 1, 2, 3, 4, 5, 6], "start": "08:00", "end": "16:00"},
                "after_hours": {"start": "08:30", "end": "11:00"}, "actions": [WEBHOOK, SLACK] if actions is None else actions, **extra}
        response = self.admin.post("/api/digests/schedules", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        with connect() as db:  # As if it was set up before the shift being tested
            db.execute("UPDATE digest_schedules SET last_period_end=? WHERE id=?", ((DAY - timedelta(hours=8)).isoformat(), created["id"]))  # The previous shift is done
        return created

    def test_a_scheduled_digest_is_made_at_shift_end_and_delivered_through_its_actions(self):
        self.fixture()
        self.shift_alarms()
        self.schedule(self.full_id)
        self.assertEqual(digests.generate_due(now=at(15)), [])  # The shift has not ended
        made = digests.generate_due(now=at(16.5))
        self.assertEqual(len(made), 1)
        self.assertEqual(digests.generate_due(now=at(17)), [])  # Not twice
        posted = []

        async def capture(url, body, headers, allow_private):
            posted.append((url, body, headers))
            return 200

        with patch("app.rule_actions._post", side_effect=capture):
            self.assertEqual(asyncio.run(digests.deliver_due()), 2)
        by_url = {url: (body, headers) for url, body, headers in posted}
        webhook_body, headers = by_url["https://8.8.8.8/hook"]
        payload = json.loads(webhook_body)
        self.assertEqual((payload["event_type"], payload["digest_id"]), ("digest.ready", made[0]))
        self.assertTrue(any(key.lower().endswith("signature") for key in headers), headers)  # Signed like every webhook
        self.assertIn("alarms", payload["headline"])
        self.assertEqual(payload["period"]["end"], at(16).isoformat())
        slack = json.loads(by_url["https://hooks.slack.com/services/T000/B000/XXXX"][0])["text"]
        self.assertIn("Day shift:", slack)
        with connect() as db:
            self.assertEqual({row["status"] for row in db.execute("SELECT status FROM digest_deliveries")}, {"delivered"})
        # The shift's digest covers its eight hours: the late alarm and gap, not the early ones.
        digest = self.admin.get(f"/api/digests/{made[0]}").json()
        self.assertEqual((digest["period_start"], digest["period_end"]), (at(8).isoformat(), at(16).isoformat()))
        self.assertEqual(digest["digest"]["gaps"]["total_seconds"], 3600.0)
        self.assertEqual({item["id"] for item in digest["digest"]["alarms"]["items"]}, set(self.in_shift.values()))  # The 03:00 to 07:00 alarms are before the shift

    def test_a_failing_destination_is_retried_and_recorded_without_losing_the_digest(self):
        self.fixture()
        self.schedule(self.full_id, actions=[WEBHOOK])
        made = digests.generate_due(now=at(17))
        with patch("app.rule_actions._post", AsyncMock(return_value=503)):
            asyncio.run(digests.deliver_due())
        with connect() as db:
            row = db.execute("SELECT status,attempt_count,last_error FROM digest_deliveries").fetchone()
        self.assertEqual((row["status"], row["attempt_count"], row["last_error"]), ("queued", 1, "http_503"))
        self.assertEqual(self.admin.get(f"/api/digests/{made[0]}").status_code, 200)

    def test_run_now_makes_the_last_shifts_digest_once(self):
        self.fixture()
        self.shift_alarms()
        created = self.schedule(self.full_id, actions=[])
        first = self.admin.post(f"/api/digests/schedules/{created['id']}/run").json()
        second = self.admin.post(f"/api/digests/schedules/{created['id']}/run").json()
        self.assertEqual((first["already_made"], second["already_made"], first["digest_id"] == second["digest_id"]), (False, True, True))

    def test_a_new_or_changed_shift_does_not_flood_with_old_digests(self):
        created = self.admin.post("/api/digests/schedules", json={"name": "New", "scope": {"site": True}, "recipient_user_id": self.full_id,
                                                                  "shift": {"days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "08:00"}, "actions": []}).json()
        self.assertEqual(digests.generate_due(), [])
        self.assertEqual(created["last_period_end"], created["last_shift_end"])

    def test_a_schedule_is_validated(self):
        base = {"name": "Bad", "scope": {"site": True}, "recipient_user_id": self.full_id, "shift": {"days": [1], "start": "08:00", "end": "16:00"}, "actions": []}
        for change in ({"scope": {"site": False}}, {"scope": {"group_ids": [str(uuid.uuid4())]}}, {"shift": {"days": [9], "start": "08:00", "end": "16:00"}},
                       {"shift": {"days": [1], "start": "8am", "end": "16:00"}}, {"recipient_user_id": 9999}, {"summarise": True},
                       {"actions": [{"type": "relay", "name": "Relay", "camera_id": self.d1}]}, {"actions": [{"type": "webhook", "name": "Loop", "url": "https://127.0.0.1/x", "signing_secret": "s" * 24}]}):
            self.assertEqual(self.admin.post("/api/digests/schedules", json={**base, **change}).status_code, 422, change)

    def test_overnight_shifts_begin_the_day_before_and_weekday_filters_apply(self):
        zone = digests.site_zone(connect().__enter__())
        start, end = digests.latest_shift({"days": [5], "start": "22:00", "end": "06:00"}, zone, at(30))  # Sunday 06:00 asked from Sunday: the Saturday-ending shift is Saturday 06:00
        self.assertEqual((start, end), (DAY - timedelta(hours=2), DAY + timedelta(hours=6)))
        start, end = digests.latest_shift({"days": [4], "start": "08:00", "end": "16:00"}, zone, at(30))  # Fridays only: Friday the 9th
        self.assertEqual(end, DAY - timedelta(hours=8))

    def test_secrets_are_never_returned(self):
        created = self.schedule(self.full_id)
        text = json.dumps(self.admin.get("/api/digests/schedules").json())
        self.assertNotIn("s" * 24, text)
        self.assertNotIn("hooks.slack.com/services", text)
        self.assertEqual({action["target"] for action in created["actions"]}, {"https://8.8.8.8", "https://hooks.slack.com"})


class ModelTests(DigestCase):
    def test_with_no_model_configured_digests_are_complete_and_nothing_goes_out(self):
        self.fixture()
        self.shift_alarms()
        self.schedule_plain()
        with patch("httpx.post", side_effect=AssertionError("no request may be made")), patch("httpx.Client.post", side_effect=AssertionError("no request may be made")), \
             patch("app.rule_actions._post", side_effect=AssertionError("no request may be made")):
            made = digests.generate_due(now=at(17))
            digest = self.admin.get(f"/api/digests/{made[0]}").json()
        self.assertIsNone(digest["summary"])
        self.assertEqual(digest["digest"]["alarms"]["total"], 2)
        self.assertEqual(self.admin.get("/api/digests/settings").json(), {"summary_enabled": False, "configured": False, "summary_model": "", "allow_private": False})

    def schedule_plain(self, **extra):
        response = self.admin.post("/api/digests/schedules", json={"name": "Plain", "scope": {"site": True}, "recipient_user_id": self.full_id,
                                                                   "shift": {"days": [0, 1, 2, 3, 4, 5, 6], "start": "08:00", "end": "16:00"}, "actions": [], **extra})
        self.assertEqual(response.status_code, 201, response.text)
        with connect() as db:
            db.execute("UPDATE digest_schedules SET last_period_end=?", ((DAY - timedelta(hours=8)).isoformat(),))
        return response.json()

    def test_a_configured_model_adds_one_paragraph_labelled_as_such_and_a_failure_costs_nothing(self):
        self.fixture()
        self.shift_alarms()
        saved = self.admin.put("/api/digests/settings", json={"enabled": True, "url": "https://8.8.8.8/v1/chat/completions", "model": "local-small"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotIn("8.8.8.8", json.dumps(saved.json()))
        self.schedule_plain(summarise=True)

        class Reply:
            def raise_for_status(self): pass
            def json(self): return {"choices": [{"message": {"content": "A quiet shift with two alarms."}}]}

        with patch("app.digests.httpx.post", return_value=Reply()) as post:
            made = digests.generate_due(now=at(17))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["model"], "local-small")
        self.assertIn("/?alarm=", post.call_args.kwargs["json"]["messages"][1]["content"])  # It is given the facts, with their links
        digest = self.admin.get(f"/api/digests/{made[0]}").json()
        self.assertEqual(digest["summary"], "A quiet shift with two alarms.")
        self.assertIn("The lists are the record", digest["summary_note"])
        self.assertIn("A quiet shift", digest["text"])
        self.assertEqual(digest["digest"]["alarms"]["total"], 2)
        with connect() as db:
            db.execute("UPDATE digest_schedules SET last_period_end=?", ((DAY - timedelta(hours=8)).isoformat(),))
            db.execute("DELETE FROM digests")
        import httpx
        with patch("app.digests.httpx.post", side_effect=httpx.ConnectError("down")):
            made = digests.generate_due(now=at(17))
        failed = self.admin.get(f"/api/digests/{made[0]}").json()
        self.assertIsNone(failed["summary"])
        self.assertIn("could not be written", failed["summary_error"])
        self.assertEqual(failed["digest"]["alarms"]["total"], 2)

    def test_the_model_endpoint_is_validated_and_needs_a_name(self):
        for body in ({"enabled": True, "url": "https://8.8.8.8/x"}, {"enabled": True, "model": "m"}, {"url": "https://127.0.0.1/x", "model": "m"},
                     {"url": "https://10.0.0.5/x", "model": "m"}, {"url": "ftp://8.8.8.8/x", "model": "m"}):
            self.assertEqual(self.admin.put("/api/digests/settings", json=body).status_code, 422, body)
        self.assertEqual(self.admin.put("/api/digests/settings", json={"enabled": True, "url": "http://10.0.0.5:11434/v1/chat/completions", "model": "m", "allow_private": True}).status_code, 200)


# Scheduled-digest tests need the schedule helper on the case that holds the fixtures.
DigestCase.schedule = ScheduleTests.schedule


if __name__ == "__main__":
    unittest.main()
