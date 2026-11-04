"""Email alerts through Microsoft sign-in (#231), against a stand-in for Microsoft's sign-in and Graph."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import ms_mail, rule_actions
from app.store import connect
from tests.fixtures.microsoft_stub import MicrosoftStub
from tests.scene_case import SceneTestCase
from tests import test_rule_actions as scenes

SECRET_WORDS = ("access-", "refresh-", "device-")


class MailCase(SceneTestCase):
    """The rule scene, with Microsoft's endpoints pointed at the stand-in."""

    rule, raise_event, deliver, log = scenes.RuleActionTests.rule, scenes.RuleActionTests.raise_event, scenes.RuleActionTests.deliver, scenes.RuleActionTests.log

    def actions(self) -> list[dict]:
        return []

    def setUp(self):
        super().setUp()
        self.microsoft = MicrosoftStub().start()
        self.addCleanup(self.microsoft.stop)
        for name in ("LOGIN_URL", "GRAPH_URL"):
            patch = mock.patch.object(ms_mail, name, self.microsoft.url + ("/v1.0" if name == "GRAPH_URL" else ""))
            patch.start()
            self.addCleanup(patch.stop)

    def connect_account(self, sender: str | None = None) -> dict:
        body = {"tenant_id": self.microsoft.tenant, "client_id": self.microsoft.client_id}
        if sender:
            body["sender"] = sender
        started = self.client.post("/api/admin/email/connect", json=body)
        self.assertEqual(started.status_code, 200, started.text)
        polled = self.client.post("/api/admin/email/poll")
        self.assertEqual(polled.status_code, 200, polled.text)
        return polled.json()

    def email_rule(self, **action) -> dict:
        return self.rule([{"type": "email", "name": "Tell the desk", "recipients": ["desk@contoso.com", "Nurse@contoso.com"], **action}])


class SignInTests(MailCase):
    def test_a_device_sign_in_gives_a_code_and_a_link_then_connects(self):
        started = self.client.post("/api/admin/email/connect", json={"tenant_id": self.microsoft.tenant, "client_id": self.microsoft.client_id}).json()
        self.assertEqual(started["state"], "pending")
        self.assertEqual((started["sign_in"]["user_code"], started["sign_in"]["verification_uri"]), ("ABCD-EFGH", "https://microsoft.com/devicelogin"))
        self.microsoft.pending_polls = 2
        self.assertEqual(self.client.post("/api/admin/email/poll").json()["state"], "pending")
        self.assertEqual(self.client.post("/api/admin/email/poll").json()["state"], "pending")
        done = self.client.post("/api/admin/email/poll").json()
        self.assertEqual((done["state"], done["account"]), ("connected", "alerts@contoso.com"))
        self.assertNotIn("sign_in", done)
        self.assertEqual(self.client.get("/api/admin/email").json()["state"], "connected")

    def test_no_token_or_code_is_ever_returned_stored_in_plain_text_or_logged(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger().debug("keep a record so assertLogs has something")
            self.connect_account()
            self.client.post("/api/admin/email/test", json={"to": "desk@contoso.com"})
            state = self.client.get("/api/admin/email").text
        for word in SECRET_WORDS:
            self.assertNotIn(word, state)
            self.assertNotIn(word, "\n".join(logs.output))
        with connect() as db:
            row = db.execute("SELECT * FROM email_connection").fetchone()
            audit = " ".join(str(item) for item in db.execute("SELECT details_json FROM audit_log").fetchall())
        raw = b"".join(bytes(row[key]) for key in ("tokens_ciphertext",) if row[key])
        self.assertTrue(raw)
        for word in SECRET_WORDS:
            self.assertNotIn(word.encode(), raw)
            self.assertNotIn(word, audit)

    def test_a_wrong_tenant_or_app_is_refused_with_a_plain_message(self):
        self.assertEqual(self.client.post("/api/admin/email/connect", json={"tenant_id": "not a tenant!", "client_id": self.microsoft.client_id}).status_code, 422)
        self.assertEqual(self.client.post("/api/admin/email/connect", json={"tenant_id": self.microsoft.tenant, "client_id": "nope"}).status_code, 422)
        wrong = self.client.post("/api/admin/email/connect", json={"tenant_id": self.microsoft.tenant, "client_id": "99999999-2222-3333-4444-555555555555"})
        self.assertEqual(wrong.status_code, 422)
        self.assertIn("Microsoft refused", wrong.json()["detail"])
        self.assertEqual(self.client.get("/api/admin/email").json()["state"], "none")

    def test_an_expired_code_ends_in_a_clear_error(self):
        self.client.post("/api/admin/email/connect", json={"tenant_id": self.microsoft.tenant, "client_id": self.microsoft.client_id})
        with connect() as db:
            from app.credentials import decrypt, encrypt
            row = db.execute("SELECT pending_ciphertext FROM email_connection").fetchone()
            pending = json.loads(decrypt(row[0]))
            pending["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            db.execute("UPDATE email_connection SET pending_ciphertext=?", (encrypt(json.dumps(pending)),))
        state = self.client.post("/api/admin/email/poll").json()
        self.assertEqual((state["state"], state["error"]["code"]), ("error", "expired_token"))

    def test_disconnect_forgets_everything_and_is_audited(self):
        self.connect_account()
        self.assertEqual(self.client.delete("/api/admin/email").status_code, 204)
        self.assertEqual(self.client.get("/api/admin/email").json()["state"], "none")
        with connect() as db:
            actions = [row[0] for row in db.execute("SELECT action FROM audit_log WHERE action LIKE 'email_%' ORDER BY id")]
        self.assertEqual(actions, ["email_connect_started", "email_connected", "email_disconnected"])


class SendTests(MailCase):
    def test_a_test_email_goes_out_through_graph_to_the_address(self):
        self.connect_account()
        self.assertEqual(self.client.post("/api/admin/email/test", json={"to": "me@contoso.com"}).status_code, 200)
        message = self.microsoft.messages[0]["message"]
        self.assertEqual([item["emailAddress"]["address"] for item in message["toRecipients"]], ["me@contoso.com"])
        self.assertEqual(self.microsoft.sent_paths, ["/v1.0/me/sendMail"])

    def test_a_shared_mailbox_sender_is_used_when_it_differs_from_the_account(self):
        self.connect_account(sender="shared@contoso.com")
        self.client.post("/api/admin/email/test", json={"to": "me@contoso.com"})
        self.assertEqual(self.microsoft.sent_paths, ["/v1.0/users/shared@contoso.com/sendMail"])

    def test_a_sender_cannot_change_the_graph_path(self):
        self.connect_account(sender="a/../../x?y#z@contoso.com")
        self.client.post("/api/admin/email/test", json={"to": "me@contoso.com"})
        self.assertEqual(self.microsoft.sent_paths, ["/v1.0/users/a%2F..%2F..%2Fx%3Fy%23z@contoso.com/sendMail"])

    def test_an_expired_access_token_is_refreshed_and_the_refresh_token_rotates(self):
        self.microsoft.expires_in = 1  # Inside the refresh margin: every use needs a new one
        self.connect_account()
        first = self.microsoft.refresh_token
        self.client.post("/api/admin/email/test", json={"to": "me@contoso.com"})
        self.assertNotEqual(self.microsoft.refresh_token, first)
        self.assertEqual([form["grant_type"] for form in self.microsoft.token_requests][-1], "refresh_token")
        self.client.post("/api/admin/email/test", json={"to": "me@contoso.com"})  # Uses the rotated token
        self.assertEqual(len(self.microsoft.messages), 2)

    def test_a_token_graph_refuses_is_replaced_once_and_the_message_still_goes(self):
        self.connect_account()
        self.microsoft.access_tokens.clear()  # Revoked server side though it has not expired
        self.assertEqual(self.client.post("/api/admin/email/test", json={"to": "me@contoso.com"}).status_code, 200)
        self.assertEqual(len(self.microsoft.messages), 1)

    def test_a_withdrawn_sign_in_says_reconnect(self):
        self.microsoft.expires_in = 1
        self.connect_account()
        self.microsoft.revoked = True
        failed = self.client.post("/api/admin/email/test", json={"to": "me@contoso.com"})
        self.assertEqual(failed.status_code, 409)
        self.assertIn("Reconnect", failed.json()["detail"])
        state = self.client.get("/api/admin/email").json()
        self.assertEqual((state["state"], state["error"]["code"], state["error"]["reconnect"]), ("error", "invalid_grant", True))

    def test_a_refusal_shows_what_microsoft_said_and_what_usually_causes_it(self):
        self.connect_account()
        self.microsoft.send_status = [(403, "ErrorAccessDenied", "Access is denied. Check credentials and try again.")]
        failed = self.client.post("/api/admin/email/test", json={"to": "me@contoso.com"})
        self.assertEqual(failed.status_code, 409)
        self.assertIn("ErrorAccessDenied: Access is denied", failed.json()["detail"])
        self.assertIn("no Exchange Online mailbox or licence", failed.json()["detail"])
        shown = self.client.get("/api/admin/email").json()["error"]  # The page shows the same, and keeps it
        self.assertEqual(shown["detail"], "ErrorAccessDenied: Access is denied. Check credentials and try again.")
        self.assertIn("ErrorAccessDenied", shown["message"])
        self.microsoft.send_status = [(404, "MailboxNotEnabledForRESTAPI", "The requested user is invalid or the mailbox is not enabled.")]
        missing = self.client.post("/api/admin/email/test", json={"to": "me@contoso.com"})
        self.assertIn("MailboxNotEnabledForRESTAPI", missing.json()["detail"])
        self.assertIn("unlicensed", missing.json()["detail"])
        self.assertEqual(self.client.post("/api/admin/email/test", json={"to": "me@contoso.com"}).status_code, 200)  # Mended: the error goes
        self.assertNotIn("error", self.client.get("/api/admin/email").json())

    def test_an_answer_with_no_error_body_still_says_the_status(self):
        self.connect_account()
        self.microsoft.send_status = [400]
        failed = self.client.post("/api/admin/email/test", json={"to": "me@contoso.com"})
        self.assertEqual(failed.status_code, 409)
        self.assertIn("Failure", failed.json()["detail"])  # Whatever code Microsoft gave

    def test_microsofts_description_of_a_sign_in_failure_is_shown_without_its_trace_ids_or_anything_token_like(self):
        self.microsoft.expires_in = 1
        self.connect_account()
        self.microsoft.revoked = True
        self.microsoft.revoked_description = "AADSTS65001: The user has not consented to Mail.Send. Trace ID: 0a1b2c3d Correlation ID: 4e5f6a7b token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
        failed = self.client.post("/api/admin/email/test", json={"to": "me@contoso.com"})
        detail = failed.json()["detail"]
        self.assertIn("AADSTS65001: The user has not consented to Mail.Send.", detail)
        self.assertIn("grant consent", detail)  # The hint for that code
        self.assertNotIn("Trace ID", detail)
        self.assertNotIn("eyJ", detail)

    def test_sending_without_a_connection_is_refused(self):
        self.assertEqual(self.client.post("/api/admin/email/test", json={"to": "me@contoso.com"}).status_code, 409)

    def test_a_bad_address_is_refused(self):
        self.connect_account()
        self.assertEqual(self.client.post("/api/admin/email/test", json={"to": "not-an-address"}).status_code, 422)


class EmailActionTests(MailCase):
    def test_a_rule_event_sends_one_email_with_the_right_recipients_and_content(self):
        self.connect_account()
        rule = self.email_rule(subject="ALERT {rule} / {camera}")
        self.raise_event(rule["id"])
        self.deliver()
        self.deliver()
        self.assertEqual(len(self.microsoft.messages), 1)
        message = self.microsoft.messages[0]["message"]
        self.assertEqual([item["emailAddress"]["address"] for item in message["toRecipients"]], ["desk@contoso.com", "nurse@contoso.com"])
        self.assertEqual(message["subject"], f"ALERT Gate entry / {self.camera_name}")
        self.assertIn(f"Vehicle on {self.camera_name}", message["body"]["content"])
        self.assertIn("No footage or camera addresses", message["body"]["content"])
        self.assertEqual([(entry["action_type"], entry["status"]) for entry in self.log(rule["id"])], [("email", "delivered")])

    def test_a_failure_is_retried_then_shown(self):
        self.connect_account()
        rule = self.email_rule()
        self.microsoft.send_status = [503]
        self.raise_event(rule["id"])
        self.deliver()
        entry = self.log(rule["id"])[0]
        self.assertEqual((entry["status"], entry["last_error"], entry["attempt_count"]), ("queued", "email_unexpected", 1))
        with connect() as db:
            db.execute("UPDATE rule_action_deliveries SET next_attempt_at=?", (datetime.now(timezone.utc).isoformat(),))
        self.deliver()
        self.assertEqual((self.log(rule["id"])[0]["status"], len(self.microsoft.messages)), ("delivered", 1))

    def test_a_refused_message_fails_at_once_and_says_why(self):
        self.connect_account()
        rule = self.email_rule()
        self.microsoft.send_status = [403]
        self.raise_event(rule["id"])
        self.deliver()
        entry = self.log(rule["id"])[0]
        self.assertEqual((entry["status"], entry["last_error"], entry["attempt_count"]), ("failed", "email_forbidden", 1))

    def test_one_noisy_rule_cannot_send_more_than_its_limit(self):
        self.connect_account()
        rule = self.email_rule()
        with mock.patch.object(rule_actions, "EMAIL_PER_RULE_PER_HOUR", 3):
            for _ in range(5):
                self.raise_event(rule["id"])
            self.deliver()
        self.assertEqual(len(self.microsoft.messages), 3)
        statuses = sorted((entry["status"], entry["last_error"]) for entry in self.log(rule["id"]))
        self.assertEqual(statuses, [("delivered", None)] * 3 + [("failed", "email_rate_limited")] * 2)

    def test_a_site_wide_limit_holds_across_rules(self):
        self.connect_account()
        first, second = self.email_rule(), self.email_rule()
        with mock.patch.object(rule_actions, "EMAIL_PER_HOUR", 2):
            for rule in (first, second, first):
                self.raise_event(rule["id"])
            self.deliver()
        self.assertEqual(len(self.microsoft.messages), 2)

    def test_the_action_needs_valid_recipients(self):
        for recipients in ([], ["nobody"], ["a@b.co"] * 21):
            response = self.client.post("/api/rules", json={
                "name": "x", "camera_ids": [self.camera_id], "classes": ["vehicle"], "condition": {"type": "zone_entry", "zone": {"x": 0, "y": 0.6, "width": 0.3, "height": 0.4}},
                "actions": [{"type": "email", "name": "Mail", "recipients": recipients}]})
            self.assertEqual(response.status_code, 422, recipients)

    def test_a_subject_cannot_carry_a_header_injection(self):
        self.connect_account()
        rule = self.email_rule(subject="Hi\r\nBcc: someone@evil.example")
        self.raise_event(rule["id"])
        self.deliver()
        self.assertNotIn("\n", self.microsoft.messages[0]["message"]["subject"])
        self.assertNotIn("\r", self.microsoft.messages[0]["message"]["subject"])


class MigrationTests(MailCase):
    def test_the_action_type_and_table_exist(self):
        with connect() as db:
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='rule_actions'").fetchone()[0]
            self.assertIn("'email'", sql)
            self.assertIn("'ptz_track'", sql)
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='email_connection'").fetchone())
            self.assertIn("last_detail", {row[1] for row in db.execute("PRAGMA table_info(email_connection)")})

