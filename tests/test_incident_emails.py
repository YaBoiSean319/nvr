"""Emails about the NVR's own problems and the summary of alert emails held back (#231), through the stand-in Microsoft sign-in and Graph."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

from app import email_outbox, incidents, rule_actions
from app.store import connect
from tests import test_email_alerts as mail


class IncidentEmailCase(mail.MailCase):
    def setUp(self):
        super().setUp()
        self.connect_account()

    def turn_on(self, recipients=("it@contoso.com", "Desk@contoso.com")):
        response = self.client.put("/api/admin/email/incidents", json={"enabled": True, "recipients": list(recipients)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def problem(self, key="camera-1", active=True, camera=True):
        with connect() as db:
            incidents.sync_incident(db, incident_key=key, camera_id=self.camera_id if camera else None, fault_type="recording", summary="Camera stopped recording", active=active)

    def send_all(self):
        while email_outbox.deliver_one():
            pass

    def outbox(self):
        with connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM email_outbox ORDER BY created_at,id")]


class SettingsTests(IncidentEmailCase):
    def test_who_is_emailed_is_saved_and_shown(self):
        saved = self.turn_on()
        self.assertEqual(saved["incidents"], {"enabled": True, "recipients": ["it@contoso.com", "desk@contoso.com"]})
        self.assertEqual(self.client.get("/api/admin/email").json()["incidents"]["recipients"], ["it@contoso.com", "desk@contoso.com"])
        self.assertEqual(len(self.audit_log("email_incidents_changed")), 1)

    def test_turning_it_on_needs_an_address_and_addresses_must_be_real(self):
        self.assertEqual(self.client.put("/api/admin/email/incidents", json={"enabled": True, "recipients": []}).status_code, 422)
        self.assertEqual(self.client.put("/api/admin/email/incidents", json={"enabled": True, "recipients": ["nobody"]}).status_code, 422)
        self.assertEqual(self.client.put("/api/admin/email/incidents", json={"enabled": False, "recipients": []}).status_code, 200)

    def audit_log(self, action):
        with connect() as db:
            return db.execute("SELECT * FROM audit_log WHERE action=?", (action,)).fetchall()


class IncidentTests(IncidentEmailCase):
    def test_a_problem_and_its_recovery_are_each_emailed_once_to_the_chosen_people(self):
        self.turn_on()
        self.problem()
        self.problem()  # Seen again: no second email
        self.send_all()
        self.assertEqual(len(self.microsoft.messages), 1)
        message = self.microsoft.messages[0]["message"]
        self.assertEqual([item["emailAddress"]["address"] for item in message["toRecipients"]], ["it@contoso.com", "desk@contoso.com"])
        self.assertEqual(message["subject"], "[NVR] Recording problem: Camera stopped recording")
        self.assertIn(f"Camera: {self.camera_name}", message["body"]["content"])
        self.assertIn("No footage or camera addresses", message["body"]["content"])
        self.problem(active=False)
        self.send_all()
        self.assertEqual(self.microsoft.messages[1]["message"]["subject"], "[NVR] Recovered: Recording problem: Camera stopped recording")

    def test_nothing_is_sent_when_it_is_off_or_nobody_is_chosen(self):
        self.problem()
        self.assertEqual(self.outbox(), [])
        self.turn_on()
        self.client.put("/api/admin/email/incidents", json={"enabled": False, "recipients": ["it@contoso.com"]})
        self.problem(key="camera-2")
        self.assertEqual(self.outbox(), [])

    def test_a_problem_during_maintenance_is_not_emailed_until_the_window_ends_if_it_is_still_there(self):
        self.turn_on()
        window = self.client.post("/api/maintenance", json={"name": "Lens clean", "scope": "cameras", "camera_ids": [self.camera_id], "duration_minutes": 60}).json()
        self.problem()
        self.assertEqual(self.outbox(), [])
        self.client.post(f"/api/maintenance/{window['id']}/end")
        self.problem()
        self.send_all()
        self.assertEqual(len(self.microsoft.messages), 1)
        self.assertIn("still there now that the window has ended", self.microsoft.messages[0]["message"]["body"]["content"])

    def test_a_failed_send_is_retried_then_shown_in_the_recent_list(self):
        self.turn_on()
        self.microsoft.send_status = [503]
        self.problem()
        self.send_all()
        row = self.outbox()[0]
        self.assertEqual((row["status"], row["last_error"], row["attempt_count"]), ("queued", "email_unexpected", 1))
        with connect() as db:
            db.execute("UPDATE email_outbox SET next_attempt_at=?", (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
        self.send_all()
        self.assertEqual(self.outbox()[0]["status"], "sent")
        recent = self.client.get("/api/admin/email").json()["recent"]
        self.assertEqual((recent[0]["status"], len(self.microsoft.messages)), ("sent", 1))

    def test_a_refused_send_is_not_retried(self):
        self.turn_on()
        self.microsoft.send_status = [403]
        self.problem()
        self.send_all()
        self.assertEqual((self.outbox()[0]["status"], self.outbox()[0]["last_error"]), ("failed", "email_forbidden"))


class HeldBackTests(IncidentEmailCase):
    def test_past_the_hourly_limit_problems_are_held_and_one_summary_says_how_many(self):
        self.turn_on()
        with mock.patch.object(email_outbox, "INCIDENT_EMAILS_PER_HOUR", 2):
            for index in range(5):
                self.problem(key=f"camera-{index}")
        self.assertEqual(len(self.outbox()), 2)
        self.assertEqual(self.client.get("/api/admin/email").json()["held_back"]["count"], 3)
        self.assertFalse(email_outbox.queue_held_summary())  # Not quiet for long enough yet
        self.assertTrue(email_outbox.queue_held_summary(datetime.now(timezone.utc) + timedelta(minutes=11)))
        self.send_all()
        subjects = [item["message"]["subject"] for item in self.microsoft.messages]
        self.assertEqual(len(subjects), 3)
        self.assertEqual(subjects[-1], "[NVR] 3 alert emails held back")
        self.assertEqual(self.client.get("/api/admin/email").json()["held_back"], {"count": 0, "since": None})
        self.assertFalse(email_outbox.queue_held_summary(datetime.now(timezone.utc) + timedelta(minutes=30)))  # Once

    def test_a_rule_email_stopped_by_its_limit_is_counted_as_held_too(self):
        rule = self.email_rule()
        with mock.patch.object(rule_actions, "EMAIL_PER_RULE_PER_HOUR", 1):
            for _ in range(3):
                self.raise_event(rule["id"])
            self.deliver()
        self.assertEqual(len(self.microsoft.messages), 1)
        self.assertEqual(self.client.get("/api/admin/email").json()["held_back"]["count"], 2)

    def test_the_summary_waits_for_somebody_to_be_chosen(self):
        with connect() as db:
            email_outbox.note_held(db)
        self.assertFalse(email_outbox.queue_held_summary(datetime.now(timezone.utc) + timedelta(hours=1)))
        self.assertEqual(self.client.get("/api/admin/email").json()["held_back"]["count"], 1)


class MigrationTests(IncidentEmailCase):
    def test_the_tables_exist(self):
        with connect() as db:
            for table in ("incident_emails", "email_outbox", "email_held"):
                self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone(), table)
