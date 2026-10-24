"""Rule actions (#113): signed webhooks, MQTT, Slack, Teams and ONVIF relay pulses."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from app import rule_actions
from app.alarms import create_alarm
from app.credentials import encrypt
from app.rules import RulesManager
from app.store import connect
from tests.fixtures.action_receivers import HttpReceiver, MqttBroker, RelayDevice
from tests.scene_case import SceneTestCase

BOTTOM_LEFT = {"x": 0.0, "y": 0.6, "width": 0.3, "height": 0.4}
SIGNING_SECRET = "s3cret-signing-key-0123456789"
MQTT_PASSWORD = "broker-pass-9876"
SLACK_TOKEN = "T000/B000/slack-path-token"
TEAMS_TOKEN = "teams-path-token-abc"


class RuleActionTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        # The local stand-ins listen on loopback; real destinations must pass the webhook rules.
        allow_local = patch("app.rule_actions.validate_destination", side_effect=lambda url, allow_private: url)
        allow_local.start()
        self.addCleanup(allow_local.stop)
        self.receiver = HttpReceiver()
        self.broker = MqttBroker(username="nvr", password=MQTT_PASSWORD)
        self.relay = RelayDevice()
        for fixture in (self.receiver, self.broker, self.relay):
            self.addCleanup(fixture.close)
        with connect() as db:
            db.execute("UPDATE cameras SET device_service_url=?,motion_credentials_ciphertext=? WHERE id=?",
                       (self.relay.url, encrypt(json.dumps({"username": "onvif", "password": "camera-pass"})), self.camera_id))

    def actions(self) -> list[dict]:
        return [
            {"type": "webhook", "name": "Ops webhook", "url": f"{self.receiver.origin}/webhook", "signing_secret": SIGNING_SECRET},
            {"type": "mqtt", "name": "Building bus", "url": f"mqtt://nvr:{MQTT_PASSWORD}@127.0.0.1:{self.broker.port}", "topic": "nvr/gate"},
            {"type": "slack", "name": "Slack #security", "url": f"{self.receiver.origin}/slack/{SLACK_TOKEN}"},
            {"type": "teams", "name": "Teams guards", "url": f"{self.receiver.origin}/teams/{TEAMS_TOKEN}"},
            {"type": "relay", "name": "Gate siren", "camera_id": self.camera_id, "duration_seconds": 0.3},
        ]

    def rule(self, actions: list[dict] | None = None, **extra) -> dict:
        response = self.client.post("/api/rules", json={
            "name": "Gate entry", "enabled": True, "camera_ids": [self.camera_id], "group_ids": [], "classes": ["vehicle"],
            "condition": {"type": "zone_entry", "zone": BOTTOM_LEFT}, "schedule": None, "cooldown_seconds": 0,
            "actions": self.actions() if actions is None else actions, **extra,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def raise_event(self, rule_id: str) -> str:
        event_id = str(uuid.uuid4())
        with connect() as db:
            db.execute(
                "INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, rule_id, "Gate entry", self.camera_id, f"track-{event_id}", "vehicle", "zone_entry",
                 datetime.now(timezone.utc).isoformat(), '{"x":0.1,"y":0.7,"width":0.2,"height":0.15}'),
            )
            create_alarm(db, event_id)
        return event_id

    def deliver(self) -> None:
        asyncio.run(rule_actions.deliver_due())

    def log(self, rule_id: str) -> list[dict]:
        response = self.client.get(f"/api/rules/{rule_id}/deliveries")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["deliveries"]

    def test_a_rule_event_runs_each_action_once(self):
        rule = self.rule()
        self.raise_event(rule["id"])
        self.deliver()
        self.deliver()

        by_path = {request["path"].split("/")[1]: request for request in self.receiver.requests}
        self.assertEqual(sorted(request["path"].split("/")[1] for request in self.receiver.requests), ["slack", "teams", "webhook"])

        # The webhook is signed like every other NVR webhook.
        webhook = by_path["webhook"]
        timestamp = webhook["headers"]["X-NVR-Timestamp"]
        expected = hmac.new(SIGNING_SECRET.encode(), timestamp.encode() + b"." + webhook["body"], hashlib.sha256).hexdigest()
        self.assertEqual(webhook["headers"]["X-NVR-Signature"], f"sha256={expected}")
        self.assertEqual(webhook["headers"]["X-NVR-Event-Type"], "rule.triggered")
        payload = json.loads(webhook["body"])
        self.assertEqual((payload["rule_name"], payload["camera_name"], payload["label"]), ("Gate entry", self.camera_name, "vehicle"))

        self.assertIn(f"Gate entry: Vehicle on {self.camera_name}", json.loads(by_path["slack"]["body"])["text"])
        self.assertIn(f"Gate entry: Vehicle on {self.camera_name}", json.loads(by_path["teams"]["body"])["text"])

        self.assertEqual(len(self.broker.messages), 1)
        message = self.broker.messages[0]
        self.assertEqual((message["topic"], message["qos"]), ("nvr/gate", 1))
        self.assertEqual(json.loads(message["payload"])["rule_name"], "Gate entry")
        self.assertEqual(self.broker.connections[0]["username"], "nvr")

        # The relay is switched on, held for the configured time, then off: the camera's first relay by default.
        self.assertEqual([(change["token"], change["state"]) for change in self.relay.changes], [("relay-1", "active"), ("relay-1", "inactive")])
        held = self.relay.changes[1]["at"] - self.relay.changes[0]["at"]
        self.assertGreaterEqual(held, 0.29)
        self.assertLess(held, 2)

        log = self.log(rule["id"])
        self.assertEqual(sorted((entry["action_type"], entry["status"], entry["attempt_count"]) for entry in log),
                         [("mqtt", "delivered", 1), ("relay", "delivered", 1), ("slack", "delivered", 1), ("teams", "delivered", 1), ("webhook", "delivered", 1)])

    def test_failed_deliveries_retry_with_backoff_and_show_in_the_log(self):
        self.receiver.statuses = [500, 200]
        rule = self.rule([{"type": "slack", "name": "Slack", "url": f"{self.receiver.origin}/slack/{SLACK_TOKEN}"}])
        self.raise_event(rule["id"])
        self.deliver()
        [entry] = self.log(rule["id"])
        self.assertEqual((entry["status"], entry["attempt_count"], entry["last_error"]), ("queued", 1, "http_500"))
        self.assertGreater(datetime.fromisoformat(entry["next_attempt_at"]), datetime.now(timezone.utc))

        self.deliver()  # Not due yet.
        self.assertEqual(len(self.receiver.requests), 1)
        with connect() as db:
            db.execute("UPDATE rule_action_deliveries SET next_attempt_at=?", (datetime.now(timezone.utc).isoformat(),))
        self.deliver()
        [entry] = self.log(rule["id"])
        self.assertEqual((entry["status"], entry["attempt_count"], entry["last_error"]), ("delivered", 2, None))

        # A refusal that retrying cannot fix fails at once.
        self.receiver.statuses = [404]
        self.raise_event(rule["id"])
        self.deliver()
        latest = self.log(rule["id"])[0]
        self.assertEqual((latest["status"], latest["attempt_count"], latest["last_error"]), ("failed", 1, "http_404"))

        # Wrong broker credentials are refused for good too.
        broker_rule = self.rule([{"type": "mqtt", "name": "Bus", "url": f"mqtt://nvr:wrong@127.0.0.1:{self.broker.port}", "topic": "nvr/x"}], name="Bus rule")
        self.raise_event(broker_rule["id"])
        self.deliver()
        self.assertEqual((self.log(broker_rule["id"])[0]["status"], self.log(broker_rule["id"])[0]["last_error"]), ("failed", "mqtt_refused_5"))

    def test_a_relay_is_always_switched_off_and_never_pulsed_twice(self):
        rule = self.rule([{"type": "relay", "name": "Siren", "camera_id": self.camera_id, "relay_token": "relay-2", "duration_seconds": 0.1}])
        self.relay.failing_releases = 2  # Switching off fails twice, then works.
        self.raise_event(rule["id"])
        self.deliver()
        self.assertEqual([(change["token"], change["state"]) for change in self.relay.changes], [("relay-2", "active"), ("relay-2", "inactive")])
        self.assertEqual(self.log(rule["id"])[0]["status"], "delivered")

        # A relay that cannot be switched off is reported, never switched on again by a retry.
        self.relay.changes.clear()
        self.relay.failing_releases = 10
        self.raise_event(rule["id"])
        self.deliver()
        self.assertEqual([change["state"] for change in self.relay.changes], ["active"])
        latest = self.log(rule["id"])[0]
        self.assertEqual((latest["status"], latest["last_error"]), ("failed", "relay_not_released"))

    def test_a_broker_that_hangs_up_is_retried(self):
        self.broker.hang_up = True
        rule = self.rule([{"type": "mqtt", "name": "Bus", "url": f"mqtt://nvr:{MQTT_PASSWORD}@127.0.0.1:{self.broker.port}", "topic": "nvr/x"}])
        self.raise_event(rule["id"])
        self.deliver()
        [entry] = self.log(rule["id"])
        self.assertEqual((entry["status"], entry["last_error"]), ("queued", "transport_IncompleteReadError"))

    def test_slack_and_teams_messages_link_the_snapshot_and_alarm(self):
        with patch.dict("os.environ", {"NVR_PUBLIC_URL": "https://nvr.example"}):
            rule = self.rule([{"type": "slack", "name": "Slack", "url": f"{self.receiver.origin}/slack/{SLACK_TOKEN}"},
                              {"type": "teams", "name": "Teams", "url": f"{self.receiver.origin}/teams/{TEAMS_TOKEN}"}])
            self.raise_event(rule["id"])
        self.deliver()
        texts = {request["path"].split("/")[1]: json.loads(request["body"])["text"] for request in self.receiver.requests}
        self.assertRegex(texts["slack"], r"<https://nvr\.example/api/push/thumbnails/[^|]+\|Snapshot> · <https://nvr\.example/\?alarm=[0-9a-f-]+\|Open alarm>")
        self.assertRegex(texts["teams"], r"\[Snapshot\]\(https://nvr\.example/api/push/thumbnails/[^)]+\) · \[Open alarm\]\(https://nvr\.example/\?alarm=")

    def test_action_secrets_never_leave_the_server(self):
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        capture = Capture(level=logging.DEBUG)
        logging.getLogger().addHandler(capture)
        self.addCleanup(logging.getLogger().removeHandler, capture)
        previous = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger().setLevel, previous)

        self.receiver.statuses = [500]
        created = self.rule()
        self.raise_event(created["id"])
        self.deliver()

        views = [
            json.dumps(created),
            self.client.get("/api/rules").text,
            self.client.get(f"/api/rules/{created['id']}").text,
            self.client.get(f"/api/rules/{created['id']}/deliveries").text,
            self.client.get("/api/diagnostics/bundle").text,
            "\n".join(records),
        ]
        for secret in (SIGNING_SECRET, MQTT_PASSWORD, SLACK_TOKEN, TEAMS_TOKEN, "camera-pass"):
            for view in views:
                self.assertNotIn(secret, view)
        slack = next(action for action in created["actions"] if action["type"] == "slack")
        self.assertEqual(slack["target"], self.receiver.origin)
        self.assertNotIn("url", slack)
        webhook = next(action for action in created["actions"] if action["type"] == "webhook")
        self.assertTrue(webhook["has_signing_secret"])

        # Saving the rule again without the secrets keeps them; a new action must bring its own.
        kept = [{key: value for key, value in action.items() if key in {"id", "type", "name", "enabled", "topic", "retain", "allow_private", "camera_id", "relay_token", "duration_seconds"}}
                for action in created["actions"]]
        payload = {key: created[key] for key in ("name", "enabled", "camera_ids", "group_ids", "condition", "classes", "schedule", "cooldown_seconds")}
        updated = self.client.put(f"/api/rules/{created['id']}", json={**payload, "actions": kept})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual([action["id"] for action in updated.json()["actions"]], [action["id"] for action in created["actions"]])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM rule_actions WHERE secret_ciphertext IS NOT NULL").fetchone()[0], 4)
        refused = self.client.put(f"/api/rules/{created['id']}", json={**payload, "actions": [*kept, {"type": "slack", "name": "No URL"}]})
        self.assertEqual(refused.status_code, 422)
        # A request the schema rejects says what is wrong without echoing what was sent.
        invalid = self.client.put(f"/api/rules/{created['id']}", json={**payload, "actions": [{"type": "slack", "url": f"https://hooks.example/{SLACK_TOKEN}"}]})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn(SLACK_TOKEN, invalid.text)
        self.assertEqual(invalid.json()["detail"][0]["loc"], ["body", "actions", 0, "name"])

    def test_test_mode_lists_the_actions_without_running_them(self):
        definition = {
            "name": "Gate entry", "enabled": True, "camera_ids": [self.camera_id], "group_ids": [], "classes": ["vehicle"],
            "condition": {"type": "zone_entry", "zone": BOTTOM_LEFT}, "schedule": None, "cooldown_seconds": 0, "actions": self.actions(),
        }
        self.analyse_footage()
        self.move_scene_into_past()
        result = self.client.post("/api/rules/test", json=definition)
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual([action["name"] for action in body["actions"]], ["Ops webhook", "Building bus", "Slack #security", "Teams guards", "Gate siren"])
        self.assertNotIn(SLACK_TOKEN, result.text)
        self.deliver()
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM rule_action_deliveries").fetchone()[0], 0)
        self.assertEqual((self.receiver.requests, self.broker.messages, self.relay.changes), ([], [], []))

    def test_actions_run_when_the_rules_worker_raises_the_event(self):
        rule = self.rule([{"type": "slack", "name": "Slack", "url": f"{self.receiver.origin}/slack/{SLACK_TOKEN}"}])
        self.analyse_footage(companions=[RulesManager(poll_seconds=0.05)])
        self.deliver()
        self.assertEqual(len(self.receiver.requests), 1)
        self.assertEqual(self.log(rule["id"])[0]["status"], "delivered")


class ActionDestinationTests(SceneTestCase):
    def save(self, action: dict):
        return self.client.post("/api/rules", json={
            "name": "Destinations", "enabled": True, "camera_ids": [self.camera_id], "group_ids": [], "classes": ["person"],
            "condition": {"type": "zone_entry", "zone": BOTTOM_LEFT}, "schedule": None, "cooldown_seconds": 0, "actions": [action],
        })

    def test_destinations_follow_the_webhook_rules(self):
        refused = [
            {"type": "slack", "name": "Plain", "url": "http://8.8.8.8/services/x"},
            {"type": "teams", "name": "Loopback", "url": "https://127.0.0.1/x"},
            {"type": "webhook", "name": "Short secret", "url": "https://8.8.8.8/hook", "signing_secret": "short"},
            {"type": "mqtt", "name": "Public plaintext", "url": "mqtt://u:p@8.8.8.8:1883", "topic": "a"},
            {"type": "mqtt", "name": "Wildcard topic", "url": "mqtts://u:p@8.8.8.8:8883", "topic": "a/#"},
            {"type": "relay", "name": "Unknown camera", "camera_id": "no-such-camera", "duration_seconds": 1},
            {"type": "relay", "name": "Too long", "camera_id": self.camera_id, "duration_seconds": 600},
        ]
        for action in refused:
            self.assertEqual(self.save(action).status_code, 422, action["name"])
        accepted = self.save({"type": "mqtt", "name": "Cloud broker", "url": "mqtts://u:p@8.8.8.8:8883", "topic": "site/alarms"})
        self.assertEqual(accepted.status_code, 201, accepted.text)
        self.assertEqual(accepted.json()["actions"][0]["target"], "mqtts://8.8.8.8:8883")
