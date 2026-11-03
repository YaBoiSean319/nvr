"""Maintenance windows (#234): what a window quiets, what it keeps, who may open one, and the announcement when it ends with a fault still there."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import alarms, incidents, maintenance
from app.store import connect
from tests.test_access_grants import GrantCase

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


class MaintenanceCase(GrantCase):
    def open(self, client, scope="cameras", camera_ids=None, group_ids=None, minutes=60, name="Lens clean", **extra):
        return client.post("/api/maintenance", json={"name": name, "scope": scope, "camera_ids": camera_ids or [], "group_ids": group_ids or [], "duration_minutes": minutes, **extra})

    def rule_alarm(self, camera_id: str, at: datetime | None = None) -> str:
        with connect() as db:
            rule = str(uuid.uuid4())
            db.execute("INSERT INTO rules(id,name,camera_ids_json,condition_type,condition_json,classes_json,active_from,pop_into_live) VALUES(?,?,?,?,?,?,?,1)",
                       (rule, "Gate", json.dumps([camera_id]), "zone_entry", '{"zone":{"x":0,"y":0,"width":1,"height":1}}', '["vehicle"]', (NOW() - timedelta(days=1)).isoformat()))
            event = str(uuid.uuid4())
            db.execute("INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json) VALUES(?,?,?,?,?,?,?,?,?)",
                       (event, rule, "Gate", camera_id, "t", "vehicle", "zone_entry", (at or NOW()).isoformat(), '{"x":0,"y":0,"width":0.1,"height":0.1}'))
            return alarms.create_alarm(db, event)

    def fault(self, camera_id: str | None, active: bool = True, key: str = "camera-1") -> None:
        with connect() as db:
            incidents.sync_incident(db, incident_key=key, camera_id=camera_id, fault_type="recording", summary="Camera offline", active=active)


class CoverageTests(MaintenanceCase):
    def test_a_window_covers_the_cameras_groups_or_site_it_names_and_only_while_it_runs(self):
        self.assertEqual(self.open(self.admin, camera_ids=[self.d1]).status_code, 201)
        with connect() as db:
            self.assertIsNotNone(maintenance.covering(db, self.d1))
            self.assertIsNone(maintenance.covering(db, self.d2))
            self.assertIsNone(maintenance.covering(db, self.d1, NOW() + timedelta(hours=2)))  # After it ended
        self.assertEqual(self.open(self.admin, scope="groups", group_ids=[self.yard], name="Yard").status_code, 201)
        with connect() as db:
            self.assertIsNotNone(maintenance.covering(db, self.y1))
            self.assertIsNone(maintenance.covering(db, self.loose))
            self.assertIsNone(maintenance.covering(db, None))  # A fault of no camera is quieted only by a site window
        self.assertEqual(self.open(self.admin, scope="site", name="Network switch").status_code, 201)
        with connect() as db:
            self.assertIsNotNone(maintenance.covering(db, self.loose))
            self.assertIsNotNone(maintenance.covering(db, None))

    def test_a_window_set_for_later_does_not_cover_now_and_is_removed_not_ended_if_cancelled(self):
        later = (NOW() + timedelta(hours=3)).isoformat()
        created = self.open(self.admin, camera_ids=[self.d1], starts_at=later, minutes=30).json()
        self.assertEqual(created["state"], "upcoming")
        with connect() as db:
            self.assertIsNone(maintenance.covering(db, self.d1))
        self.assertEqual(self.admin.post(f"/api/maintenance/{created['id']}/end").json()["state"], "removed")
        self.assertEqual(self.admin.get("/api/maintenance").json()["windows"], [])


class AlarmTests(MaintenanceCase):
    def test_during_a_window_an_alarm_is_kept_and_marked_but_nothing_goes_out(self):
        window = self.open(self.admin, camera_ids=[self.d1]).json()
        with mock.patch("app.webhooks.enqueue_event") as webhook, mock.patch("app.push.queue_for_alarm") as push, mock.patch("app.rule_actions.queue_for_event") as actions:
            alarm_id = self.rule_alarm(self.d1)
        webhook.assert_not_called()
        push.assert_not_called()
        actions.assert_not_called()
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM alarm_pop_ins").fetchone()[0], 0)
        shown = self.admin.get(f"/api/alarms/{alarm_id}").json()
        self.assertEqual(shown["maintenance"], {"id": window["id"], "name": "Lens clean"})
        self.assertEqual(self.admin.get("/api/maintenance").json()["windows"][0]["suppressed_alarms"], 1)

    def test_an_alarm_on_another_camera_or_after_the_window_goes_out_as_usual(self):
        window = self.open(self.admin, camera_ids=[self.d1]).json()
        with mock.patch("app.webhooks.enqueue_event") as webhook, mock.patch("app.push.queue_for_alarm") as push, mock.patch("app.rule_actions.queue_for_event") as actions:
            other = self.rule_alarm(self.d2)
            self.assertEqual((webhook.call_count, push.call_count, actions.call_count), (1, 1, 1))
            self.assertEqual(self.admin.post(f"/api/maintenance/{window['id']}/end").status_code, 200)
            self.rule_alarm(self.d1)
            self.assertEqual((webhook.call_count, push.call_count, actions.call_count), (2, 2, 2))
        self.assertIsNone(self.admin.get(f"/api/alarms/{other}").json().get("maintenance"))


class IncidentTests(MaintenanceCase):
    def test_a_fault_during_a_window_raises_no_webhook_and_is_announced_when_it_ends_if_still_there(self):
        window = self.open(self.admin, camera_ids=[self.d1]).json()
        with mock.patch("app.webhooks.enqueue_event") as webhook:
            self.fault(self.d1)
            self.fault(self.d1)  # Seen again while the window runs: still quiet
            webhook.assert_not_called()
            with connect() as db:
                self.assertEqual(db.execute("SELECT held_by_window FROM incidents").fetchone()[0], window["id"])
            self.assertEqual(self.admin.post(f"/api/maintenance/{window['id']}/end").status_code, 200)
            self.fault(self.d1)  # The next look after the window: it is still broken, so it is announced now
            self.assertEqual(webhook.call_count, 1)
            self.assertEqual(webhook.call_args.args[1], "recording.failure")
            self.fault(self.d1)
            self.assertEqual(webhook.call_count, 1)  # Once
        with connect() as db:
            self.assertIsNone(db.execute("SELECT held_by_window FROM incidents").fetchone()[0])

    def test_a_fault_that_is_gone_when_the_window_ends_is_never_announced(self):
        window = self.open(self.admin, camera_ids=[self.d1]).json()
        with mock.patch("app.webhooks.enqueue_event") as webhook:
            self.fault(self.d1)
            self.fault(self.d1, active=False)  # Mended during the work: no recovery notice for something never announced
            self.assertEqual(self.admin.post(f"/api/maintenance/{window['id']}/end").status_code, 200)
            self.fault(self.d1, active=False)
            webhook.assert_not_called()

    def test_a_fault_of_no_camera_waits_for_a_site_window(self):
        self.open(self.admin, camera_ids=[self.d1])
        with mock.patch("app.webhooks.enqueue_event") as webhook:
            self.fault(None, key="worker")
            self.assertEqual(webhook.call_count, 1)  # A window for one camera does not quiet the site
            self.open(self.admin, scope="site", name="Power work")
            self.fault(None, key="storage")
            self.assertEqual(webhook.call_count, 1)

    def test_a_fault_on_another_camera_is_not_quieted(self):
        self.open(self.admin, camera_ids=[self.d1])
        with mock.patch("app.webhooks.enqueue_event") as webhook:
            self.fault(self.d2, key="camera-2")
            self.assertEqual(webhook.call_count, 1)


class AccessTests(MaintenanceCase):
    def test_an_operator_may_open_a_short_window_on_cameras_they_can_see(self):
        made = self.open(self.limited, camera_ids=[self.d1], minutes=120)
        self.assertEqual(made.status_code, 201, made.text)
        self.assertEqual(made.json()["created_by"], "limited")
        self.assertEqual([item["action"] for item in [self.audit("maintenance_opened")[0]]], ["maintenance_opened"])

    def test_an_operator_cannot_cover_cameras_they_cannot_see_groups_the_site_or_a_long_time(self):
        self.assertEqual(self.open(self.limited, camera_ids=[self.y1]).status_code, 422)  # Hidden camera
        self.assertEqual(self.open(self.limited, camera_ids=[str(uuid.uuid4())]).status_code, 422)  # Unknown: the same answer
        self.assertEqual(self.open(self.limited, scope="groups", group_ids=[self.dock]).status_code, 422)
        self.assertEqual(self.open(self.limited, scope="site").status_code, 422)
        self.assertEqual(self.open(self.limited, camera_ids=[self.d1], minutes=9 * 60).status_code, 422)
        later = (NOW() + timedelta(days=2)).isoformat()
        self.assertEqual(self.open(self.limited, camera_ids=[self.d1], starts_at=later).status_code, 422)

    def test_administrators_are_limited_to_a_week_and_to_a_start_not_in_the_past(self):
        self.assertEqual(self.open(self.admin, scope="site", minutes=7 * 24 * 60).status_code, 201)
        self.assertEqual(self.open(self.admin, scope="site", minutes=7 * 24 * 60 + 1).status_code, 422)
        self.assertEqual(self.open(self.admin, scope="site", starts_at=(NOW() - timedelta(hours=1)).isoformat()).status_code, 422)
        self.assertEqual(self.open(self.admin, camera_ids=[self.d1], minutes=1, ends_at=None).status_code, 201)

    def test_operators_see_only_windows_about_their_cameras_and_end_only_their_own(self):
        mine = self.open(self.limited, camera_ids=[self.d1]).json()
        theirs = self.open(self.admin, camera_ids=[self.y1], name="Yard camera").json()
        site = self.open(self.admin, scope="site", name="Power").json()
        shown = {item["id"]: item for item in self.limited.get("/api/maintenance").json()["windows"]}
        self.assertEqual(set(shown), {mine["id"], site["id"]})
        self.assertEqual((shown[mine["id"]]["can_end"], shown[site["id"]]["can_end"]), (True, False))  # Their own, and the administrator's
        self.assertTrue(all(item["can_end"] for item in self.admin.get("/api/maintenance").json()["windows"]))
        self.assertEqual(self.limited.post(f"/api/maintenance/{theirs['id']}/end").status_code, 404)
        self.assertEqual(self.limited.post(f"/api/maintenance/{site['id']}/end").status_code, 404)
        self.assertEqual(self.limited.post(f"/api/maintenance/{mine['id']}/end").status_code, 200)
        self.assertEqual(self.admin.post(f"/api/maintenance/{theirs['id']}/end").status_code, 200)
        self.assertEqual(self.admin.post(f"/api/maintenance/{theirs['id']}/end").status_code, 409)  # Already ended

    def test_the_list_says_which_cameras_are_in_maintenance_for_this_person(self):
        self.open(self.admin, camera_ids=[self.d1, self.y1])
        self.assertEqual(set(self.admin.get("/api/maintenance").json()["covered_camera_ids"]), {self.d1, self.y1})
        self.assertEqual(set(self.limited.get("/api/maintenance").json()["covered_camera_ids"]), {self.d1})

    def test_ending_and_opening_are_audited_with_what_was_held(self):
        window = self.open(self.admin, camera_ids=[self.d1]).json()
        with mock.patch("app.webhooks.enqueue_event"), mock.patch("app.push.queue_for_alarm"), mock.patch("app.rule_actions.queue_for_event"):
            self.rule_alarm(self.d1)
            self.fault(self.d1)
        self.admin.post(f"/api/maintenance/{window['id']}/end")
        ended = self.audit("maintenance_ended")[0]["details"]
        self.assertEqual((ended["held_alarms"], ended["held_incidents"], ended["early"]), (1, 1, True))
        self.assertEqual(len(self.audit("maintenance_opened")), 1)


class MigrationTests(MaintenanceCase):
    def test_the_table_and_the_marker_columns_exist(self):
        with connect() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_windows'").fetchone())
            self.assertIn("maintenance_window_id", {row[1] for row in db.execute("PRAGMA table_info(alarms)")})
            self.assertIn("held_by_window", {row[1] for row in db.execute("PRAGMA table_info(incidents)")})
