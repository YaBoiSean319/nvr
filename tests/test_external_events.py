"""External events in (#138): signed events, replays and forgeries refused, mapping to cameras, the timeline, rules, MQTT, Home Assistant, and secrets."""
from __future__ import annotations

import asyncio
import json
import re
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import external_events, home_assistant, mqtt
from app.store import connect
from tests.fixtures.action_receivers import MqttBroker
from tests.test_access_grants import GrantCase

SECRET = "door-system-secret-0123456789"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


class EventCase(GrantCase):
    def tearDown(self):
        for broker in getattr(self, "brokers", []):
            broker.close()
        super().tearDown()

    def broker(self, **options) -> MqttBroker:
        broker = MqttBroker(**options)
        self.brokers = [*getattr(self, "brokers", []), broker]
        return broker

    def http_source(self, mappings=None, **extra) -> str:
        body = {"name": "Door controller", "kind": "http", "secret": SECRET, "mappings": mappings if mappings is not None else [{"subject": "main-door", "camera_ids": [self.d1, self.d2]}], **extra}
        response = self.admin.post("/api/event-sources", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def send(self, source_id: str, event: dict, *, secret: str = SECRET, timestamp: int | None = None, client: TestClient | None = None, raw: bytes | None = None, headers=None):
        body = raw if raw is not None else json.dumps(event).encode()
        signed = external_events.sign(secret, body, timestamp)
        return (client or TestClient(self.app)).post(f"/api/inbound/events/{source_id}", content=body, headers={"Content-Type": "application/json", **signed, **(headers or {})})

    def event(self, **extra) -> dict:
        return {"event_id": str(uuid.uuid4()), "type": "door_forced", "subject": "main-door", "message": "Forced open", **extra}

    def timeline(self, camera_id: str, client=None) -> list[dict]:
        response = (client or self.admin).post("/api/playback/timeline", json={"camera_ids": [camera_id], "start": (NOW - timedelta(hours=1)).isoformat(), "end": (NOW + timedelta(hours=1)).isoformat()})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["cameras"][0]["external_events"]


class InboundTests(EventCase):
    def test_a_signed_event_is_accepted_kept_and_tied_to_the_mapped_cameras(self):
        source = self.http_source()
        response = self.send(source, self.event(data={"badge": "A-17"}))  # Needs no sign-in: the signature is the credential
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual((response.json()["cameras"], response.json()["alarms"]), (2, 0))
        events = self.admin.get("/api/external-events", params={"camera_ids": [self.d1]}).json()["events"]
        self.assertEqual([(item["type"], item["subject"], item["label"], sorted(item["camera_ids"])) for item in events], [("door_forced", "main-door", "Door forced open", [self.d1])])
        self.assertEqual(len(self.timeline(self.d1)), 1)
        self.assertEqual(self.timeline(self.d1)[0]["message"], "Forced open")
        self.assertEqual(self.timeline(self.y1), [])  # Not mapped to it

    def test_unsigned_wrongly_signed_stale_and_replayed_events_are_refused(self):
        source = self.http_source()
        event = self.event()
        body = json.dumps(event).encode()
        self.assertEqual(TestClient(self.app).post(f"/api/inbound/events/{source}", content=body).status_code, 401)  # Unsigned
        self.assertEqual(self.send(source, event, secret="not-the-secret-0123456789").status_code, 401)  # Wrong secret
        self.assertEqual(self.send(source, event, timestamp=int(time.time()) - 3600).status_code, 401)  # Stale, though validly signed once
        self.assertEqual(self.send(source, event, raw=body, headers={"X-NVR-Signature": "sha256=" + "0" * 64}).status_code, 401)
        self.assertEqual(self.send(source, event, headers={"X-NVR-Signature": "md5=abc"}).status_code, 401)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM external_events").fetchone()[0], 0)  # None of those was kept
        first = self.send(source, event)
        self.assertEqual(first.status_code, 202)
        replay = self.send(source, event)  # The same event again, freshly signed: the sender's id says it is a repeat
        self.assertEqual(replay.status_code, 409)
        exact = TestClient(self.app).post(f"/api/inbound/events/{source}", content=body, headers={"Content-Type": "application/json", **external_events.sign(SECRET, body, 1_700_000_000)})
        self.assertEqual(exact.status_code, 401)  # A captured request replayed later is stale
        self.assertEqual(len(self.timeline(self.d1)), 1)

    def test_an_event_without_an_id_is_still_refused_when_replayed_exactly(self):
        source = self.http_source()
        body = json.dumps({"type": "panic", "subject": "main-door"}).encode()
        headers = {"Content-Type": "application/json", **external_events.sign(SECRET, body)}
        client = TestClient(self.app)
        self.assertEqual(client.post(f"/api/inbound/events/{source}", content=body, headers=headers).status_code, 202)
        self.assertEqual(client.post(f"/api/inbound/events/{source}", content=body, headers=headers).status_code, 409)  # The signature is its identity

    def test_an_unknown_or_switched_off_source_is_refused_like_a_bad_signature(self):
        source = self.http_source(enabled=False)
        refused = [self.send(source, self.event()), self.send(str(uuid.uuid4()), self.event()), self.send(source, self.event(), secret="x" * 24)]
        self.assertEqual({(item.status_code, item.text) for item in refused}, {(401, '{"detail":"Not accepted"}')})  # Nothing says which sources exist

    def test_a_trusted_sender_is_told_what_is_wrong_with_its_event(self):
        source = self.http_source()
        for event, text in (({"subject": "x"}, "type is required"), ({"type": "Bad Type!"}, "type is required"), ({"type": "panic", "data": "x"}, "data must be an object"),
                            ({"type": "panic", "occurred_at": "yesterday"}, "occurred_at must be a timestamp"),
                            ({"type": "panic", "occurred_at": (NOW + timedelta(hours=2)).isoformat()}, "not in the future"),
                            ({"type": "panic", "occurred_at": (NOW - timedelta(days=30)).isoformat()}, "last 7 days")):
            response = self.send(source, event)
            self.assertEqual(response.status_code, 422, event)
            self.assertIn(text, response.json()["detail"])
        self.assertEqual(self.send(source, {}, raw=b"not json").status_code, 422)
        self.assertEqual(self.send(source, {}, raw=b"[]").status_code, 422)
        self.assertEqual(self.send(source, {}, raw=b"x" * 20_000).status_code, 413)
        chunked = TestClient(self.app).post(f"/api/inbound/events/{source}", content=(b"x" * 4096 for _ in range(8)), headers={"Content-Type": "application/json"})
        self.assertEqual(chunked.status_code, 413)  # No length declared, still capped

    def test_an_event_from_the_past_is_placed_when_it_happened(self):
        source = self.http_source()
        self.send(source, self.event(occurred_at=(NOW - timedelta(minutes=20)).isoformat()))
        [event] = self.timeline(self.d1)
        self.assertEqual(event["occurred_at"], (NOW - timedelta(minutes=20)).isoformat())

    def test_a_runaway_sender_is_slowed(self):
        source = self.http_source()
        from app.api import external_events as api
        api._recent.clear()
        with patch.object(api, "RATE_PER_MINUTE", 3):
            codes = [self.send(source, self.event()).status_code for _ in range(5)]
        self.assertEqual(codes, [202, 202, 202, 429, 429])
        api._recent.clear()


class MappingTests(EventCase):
    def test_events_are_tied_to_cameras_by_subject_and_type_and_unmapped_ones_are_kept_for_the_administrator(self):
        source = self.http_source(mappings=[{"subject": "main-door", "types": ["door_forced"], "camera_ids": [self.d1]}, {"subject": "*", "types": ["panic"], "camera_ids": [self.y1]}])
        self.send(source, self.event(type="door_forced"))
        self.send(source, self.event(type="door_granted"))                      # Not a type mapped for that door
        self.send(source, self.event(type="panic", subject="lobby-button"))     # Any subject, one type
        self.send(source, self.event(type="door_forced", subject="back-door"))  # Nothing maps it
        self.assertEqual([item["type"] for item in self.timeline(self.d1)], ["door_forced"])
        self.assertEqual([item["type"] for item in self.timeline(self.y1)], ["panic"])
        listed = self.admin.get(f"/api/event-sources/{source}/events").json()["events"]
        self.assertEqual(sorted((item["type"], item["subject"], item["mapped"]) for item in listed),
                         [("door_forced", "back-door", False), ("door_forced", "main-door", True), ("door_granted", "main-door", False), ("panic", "lobby-button", True)])

    def test_mappings_are_validated_and_replaced(self):
        source = self.http_source()
        for bad in ([{"subject": "", "camera_ids": [self.d1]}], [{"subject": "a", "camera_ids": [str(uuid.uuid4())]}], [{"subject": "a", "camera_ids": [self.d1]}, {"subject": "a", "camera_ids": [self.d2]}],
                    [{"subject": "a", "types": ["Not Valid!"], "camera_ids": [self.d1]}]):
            self.assertEqual(self.admin.put(f"/api/event-sources/{source}/mappings", json=bad).status_code, 422, bad)
        replaced = self.admin.put(f"/api/event-sources/{source}/mappings", json=[{"subject": "side-door", "camera_ids": [self.y1]}]).json()["mappings"]
        self.assertEqual([item["subject"] for item in replaced], ["side-door"])
        self.assertEqual(self.admin.put(f"/api/event-sources/{uuid.uuid4()}/mappings", json=[]).status_code, 404)

    def test_an_archived_camera_is_not_mapped_to(self):
        source = self.http_source(mappings=[{"subject": "main-door", "camera_ids": [self.d1]}])
        with connect() as db:
            db.execute("UPDATE cameras SET archived_at=? WHERE id=?", (NOW.isoformat(), self.d1))
        self.assertEqual(self.send(source, self.event()).json()["cameras"], 0)


class RuleTests(EventCase):
    def rule(self, **condition) -> str:
        body = {"name": "Door forced", "enabled": True, "camera_ids": [self.d1], "group_ids": [], "condition": {"type": "external_event", "types": ["door_forced"], **condition},
                "classes": ["person"], "schedule": None, "cooldown_seconds": 0, "pop_into_live": False, "actions": []}
        response = self.admin.post("/api/rules", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["classes"], [])  # An event rule is not about objects
        return response.json()["id"]

    def alarms(self) -> list[dict]:
        return self.admin.get("/api/alarms", params={"state": "open"}).json()["alarms"]

    def test_a_mapped_event_raises_an_alarm_through_a_rule_that_asks_for_it(self):
        source = self.http_source()
        rule = self.rule()
        self.send(source, self.event(type="door_granted"))  # Not the type the rule asks for
        self.assertEqual(self.alarms(), [])
        self.assertEqual(self.send(source, self.event()).json()["alarms"], 1)
        [alarm] = self.alarms()
        self.assertEqual((alarm["rule_id"], alarm["camera_id"], alarm["label"], alarm["condition_type"]), (rule, self.d1, "door_forced", "external_event"))

    def test_a_rule_can_be_narrowed_by_source_and_subject_and_obeys_cooldown(self):
        source = self.http_source(mappings=[{"subject": "*", "camera_ids": [self.d1]}])
        other = self.admin.post("/api/event-sources", json={"name": "Other", "kind": "http", "secret": SECRET + "2", "mappings": [{"subject": "*", "camera_ids": [self.d1]}]}).json()["id"]
        self.rule(source_ids=[source], subjects=["main-door"], types=[])
        self.send(other, self.event(), secret=SECRET + "2")     # Wrong source
        self.send(source, self.event(subject="back-door"))      # Wrong subject
        self.assertEqual(self.alarms(), [])
        self.send(source, self.event())
        self.assertEqual(len(self.alarms()), 1)
        cooling = self.admin.post("/api/rules", json={"name": "Cool", "camera_ids": [self.d2], "condition": {"type": "external_event"}, "cooldown_seconds": 600}).json()["id"]
        with connect() as db:
            db.execute("INSERT INTO event_mappings(id,source_id,subject,types_json,camera_ids_json) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), other, "cool", "[]", json.dumps([self.d2])))
        self.send(other, self.event(subject="cool"), secret=SECRET + "2")
        self.send(other, self.event(subject="cool"), secret=SECRET + "2")
        self.assertEqual(len([alarm for alarm in self.alarms() if alarm["rule_id"] == cooling]), 1)

    def test_test_mode_replays_the_events_that_were_kept(self):
        source = self.http_source()
        self.send(source, self.event())
        self.send(source, self.event(type="door_granted"))
        response = self.admin.post("/api/rules/test", json={"name": "Door forced", "camera_ids": [self.d1], "condition": {"type": "external_event", "types": ["door_forced"]}})
        self.assertEqual(response.status_code, 200, response.text)
        events = response.json()["events"]
        self.assertEqual([item["label"] for item in events], ["door_forced"])


class SecretTests(EventCase):
    def test_sources_and_secrets_are_for_administrators_and_secrets_are_never_returned(self):
        source = self.http_source()
        mqtt_source = self.admin.post("/api/event-sources", json={"name": "Broker", "kind": "mqtt", "url": "mqtt://door:hunter2broker@192.168.7.9:1883", "topic": "building/#", "allow_private": True}).json()["id"]
        text = json.dumps(self.admin.get("/api/event-sources").json())
        for secret in (SECRET, "hunter2broker"):
            self.assertNotIn(secret, text)
        listed = {item["id"]: item for item in self.admin.get("/api/event-sources").json()["sources"]}
        self.assertTrue(listed[source]["has_secret"])
        self.assertEqual(listed[mqtt_source]["target"], "mqtt://192.168.7.9:1883")  # Where it goes, without the login
        for client in (self.limited, self.full):
            self.assertEqual(client.get("/api/event-sources").status_code, 403)
            self.assertEqual(client.post("/api/event-sources", json={"name": "x", "kind": "http", "secret": SECRET}).status_code, 403)
            self.assertEqual(client.put(f"/api/event-sources/{source}", json={"name": "x", "kind": "http"}).status_code, 403)
            self.assertEqual(client.delete(f"/api/event-sources/{source}").status_code, 403)
            self.assertEqual(client.get(f"/api/event-sources/{source}/events").status_code, 403)
            self.assertEqual(client.get("/api/home-assistant").status_code, 403)
        with connect() as db:  # At rest they are encrypted
            raw = db.execute("SELECT secret_ciphertext FROM event_sources WHERE id=?", (source,)).fetchone()[0]
        self.assertNotIn(SECRET.encode(), raw)

    def test_a_secret_left_out_is_kept_and_one_sent_replaces_it(self):
        source = self.http_source()
        self.assertEqual(self.admin.put(f"/api/event-sources/{source}", json={"name": "Renamed", "kind": "http"}).status_code, 200)
        self.assertEqual(self.send(source, self.event()).status_code, 202)  # Still the same secret
        self.assertEqual(self.admin.put(f"/api/event-sources/{source}", json={"name": "Renamed", "kind": "http", "secret": "a-brand-new-secret-9876543"}).status_code, 200)
        self.assertEqual(self.send(source, self.event()).status_code, 401)
        self.assertEqual(self.send(source, self.event(), secret="a-brand-new-secret-9876543").status_code, 202)

    def test_sources_are_validated(self):
        for body in ({"name": "x", "kind": "http"}, {"name": "x", "kind": "http", "secret": "short"}, {"name": "x", "kind": "mqtt", "topic": "a/#"}, {"name": "x", "kind": "mqtt", "url": "mqtt://8.8.8.8:1883"},
                     {"name": "x", "kind": "mqtt", "url": "mqtt://8.8.8.8:1883", "topic": "a/#/b"}, {"name": "x", "kind": "mqtt", "url": "mqtt://127.0.0.1:1883", "topic": "a/#"},
                     {"name": "x", "kind": "home_assistant", "secret": SECRET}, {"name": " ", "kind": "http", "secret": SECRET}):
            self.assertEqual(self.admin.post("/api/event-sources", json=body).status_code, 422, body)

    def test_changes_to_sources_are_audited_and_a_deleted_source_stops_being_accepted(self):
        source = self.http_source()
        self.assertEqual(self.admin.delete(f"/api/event-sources/{source}").status_code, 204)
        self.assertEqual(self.send(source, self.event()).status_code, 401)
        with connect() as db:
            actions = [row[0] for row in db.execute("SELECT action FROM audit_log WHERE resource_type='event_source' ORDER BY id")]
        self.assertEqual(actions, ["event_source_created", "event_source_deleted"])


class AccessTests(EventCase):
    def test_a_person_sees_only_events_on_cameras_they_may_see(self):
        source = self.http_source(mappings=[{"subject": "main-door", "camera_ids": [self.d1, self.y1]}])
        self.send(source, self.event())
        self.assertEqual({camera for item in self.admin.get("/api/external-events").json()["events"] for camera in item["camera_ids"]}, {self.d1, self.y1})
        limited = self.limited.get("/api/external-events").json()["events"]
        self.assertEqual([item["camera_ids"] for item in limited], [[self.d1]])  # The hidden camera is not even named
        self.assertEqual(self.limited.get("/api/external-events", params={"camera_ids": [self.y1]}).json()["events"], [])
        self.assertEqual(self.limited.post("/api/playback/timeline", json={"camera_ids": [self.y1], "start": (NOW - timedelta(hours=1)).isoformat(), "end": (NOW + timedelta(hours=1)).isoformat()}).status_code, 422)


class MqttTests(EventCase):
    def run_manager(self, broker: MqttBroker, deliveries: list[tuple[str, bytes]], seconds: float = 1.5, wait_for_subscription: bool = True) -> None:
        async def run() -> None:
            manager = external_events.ExternalEventManager(reconcile_seconds=0.1)
            await manager.start()
            for _ in range(60):
                await asyncio.sleep(0.1)
                if broker.subscriptions or not wait_for_subscription:
                    break
            for topic, payload in deliveries:
                broker.deliver(topic, payload)
            await asyncio.sleep(seconds)
            await manager.stop()
        asyncio.run(run())

    def mqtt_source(self, broker: MqttBroker, **extra) -> str:
        body = {"name": "Door bus", "kind": "mqtt", "url": f"mqtt://door:pw@127.0.0.1:{broker.port}", "topic": "building/doors/#", "allow_private": True, **extra}
        with patch("app.rule_actions._check_broker", lambda url, allow_private, delivering=False: mqtt.parse_broker(url)):
            response = self.admin.post("/api/event-sources", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_events_published_to_the_broker_arrive_mapped_and_trigger_rules(self):
        broker = self.broker(username="door", password="pw")
        source = self.mqtt_source(broker)
        self.admin.put(f"/api/event-sources/{source}/mappings", json=[{"subject": "building/doors/main", "camera_ids": [self.d1]}])
        self.admin.post("/api/rules", json={"name": "Door", "camera_ids": [self.d1], "condition": {"type": "external_event"}})
        self.run_manager(broker, [("building/doors/main", json.dumps({"type": "door_forced", "message": "Forced"}).encode()), ("building/doors/main", b"unlocked"), ("other/topic", b"ignored")])
        events = self.admin.get(f"/api/event-sources/{source}/events").json()["events"]
        self.assertEqual(sorted((item["type"], item["subject"], item["mapped"]) for item in events), [("door_forced", "building/doors/main", True), ("mqtt_message", "building/doors/main", True)])
        self.assertEqual({item["type"] for item in self.timeline(self.d1)}, {"door_forced", "mqtt_message"})
        self.assertEqual(len(self.admin.get("/api/alarms", params={"state": "open"}).json()["alarms"]), 2)
        self.assertEqual(broker.connections[0]["username"], "door")

    def test_a_retained_message_is_not_taken_as_a_new_event(self):
        broker = self.broker(username="door", password="pw")
        broker.retained["building/doors/main"] = {"topic": "building/doors/main", "payload": json.dumps({"type": "door_forced"}).encode(), "qos": 0, "retain": True}
        source = self.mqtt_source(broker)
        self.run_manager(broker, [], seconds=1.0)
        self.assertEqual(self.admin.get(f"/api/event-sources/{source}/events").json()["events"], [])

    def test_editing_an_mqtt_source_keeps_its_private_network_setting(self):
        broker = self.broker(username="door", password="pw")
        source = self.mqtt_source(broker)
        self.admin.put(f"/api/event-sources/{source}", json={"name": "Renamed", "kind": "mqtt", "topic": "building/doors/#"})
        listed = {item["id"]: item for item in self.admin.get("/api/event-sources").json()["sources"]}
        self.assertTrue(listed[source]["allow_private"])

    def test_a_broker_that_refuses_the_login_is_reported_not_retried_hard(self):
        broker = self.broker(username="door", password="right")
        source = self.mqtt_source(broker)
        with connect() as db:
            from app.credentials import encrypt
            db.execute("UPDATE event_sources SET secret_ciphertext=? WHERE id=?", (encrypt(f"mqtt://door:wrong@127.0.0.1:{broker.port}"), source))
        self.run_manager(broker, [], seconds=1.0, wait_for_subscription=False)
        error = self.admin.get("/api/event-sources").json()["sources"][0]["last_error"]
        self.assertIn("refused the login", error)
        self.assertEqual(len(broker.connections), 1)  # And waited before trying again

    def test_a_switched_off_source_is_not_listened_to(self):
        broker = self.broker()
        source = self.mqtt_source(broker, enabled=False)
        self.run_manager(broker, [("building/doors/main", b"x")], seconds=0.5, wait_for_subscription=False)
        self.assertEqual(broker.connections, [])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM external_events WHERE source_id=?", (source,)).fetchone()[0], 0)


# --- Home Assistant: its discovery schema, in a broker test -------------------------------------------------------------------------------

DEVICE_KEYS = {"configuration_url", "connections", "hw_version", "identifiers", "manufacturer", "model", "name", "suggested_area", "sw_version", "via_device"}
COMMON_KEYS = {"availability", "availability_mode", "availability_template", "availability_topic", "device", "enabled_by_default", "encoding", "entity_category", "icon",
               "json_attributes_template", "json_attributes_topic", "name", "object_id", "payload_available", "payload_not_available", "unique_id"}
SCHEMA = {  # The keys Home Assistant's MQTT discovery accepts for each component, and the ones it requires
    "camera": ({"topic", "image_encoding"} | COMMON_KEYS, {"topic"}),
    "binary_sensor": ({"state_topic", "payload_on", "payload_off", "device_class", "expire_after", "force_update", "off_delay", "value_template"} | COMMON_KEYS, {"state_topic"}),
    "sensor": ({"state_topic", "unit_of_measurement", "device_class", "state_class", "value_template", "expire_after", "force_update", "last_reset_value_template"} | COMMON_KEYS, {"state_topic"}),
}
BINARY_SENSOR_CLASSES = {"battery", "battery_charging", "carbon_monoxide", "cold", "connectivity", "door", "garage_door", "gas", "heat", "light", "lock", "moisture", "motion", "moving", "occupancy",
                         "opening", "plug", "power", "presence", "problem", "running", "safety", "smoke", "sound", "tamper", "update", "vibration", "window"}
TOPIC = re.compile(r"^(?P<prefix>[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)/(?P<component>[a-z_]+)/(?P<node>[A-Za-z0-9_-]+)/(?P<object>[A-Za-z0-9_-]+)/config$")


class HomeAssistantTests(MqttTests):
    def enable(self, broker: MqttBroker, **extra) -> dict:
        with patch("app.rule_actions._check_broker", lambda url, allow_private, delivering=False: mqtt.parse_broker(url)):
            response = self.admin.put("/api/home-assistant", json={"enabled": True, "url": f"mqtt://ha:pw@127.0.0.1:{broker.port}", "allow_private": True, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def sync(self, manager: home_assistant.HomeAssistantManager | None = None) -> home_assistant.HomeAssistantManager:
        manager = manager or home_assistant.HomeAssistantManager()
        asyncio.run(manager.sync())
        return manager

    def retained(self, broker: MqttBroker) -> dict[str, str]:
        return {message["topic"]: message["payload"].decode("latin-1") for message in broker.messages if message["retain"]}

    def test_discovery_messages_match_home_assistants_schema(self):
        broker = self.broker(username="ha", password="pw")
        self.enable(broker)
        self.sync()
        retained = self.retained(broker)
        configs = {topic: json.loads(payload) for topic, payload in retained.items() if topic.endswith("/config")}
        components = []
        for topic, config in configs.items():
            match = TOPIC.match(topic)
            self.assertIsNotNone(match, topic)
            self.assertEqual((match["prefix"], match["node"]), ("homeassistant", "nvr"))
            component = match["component"]
            components.append(component)
            allowed, required = SCHEMA[component]
            self.assertLessEqual(set(config) - allowed, set(), f"{topic}: keys Home Assistant would reject")
            self.assertGreaterEqual(set(config), required | {"unique_id", "name", "device"}, topic)
            self.assertLessEqual(set(config["device"]) - DEVICE_KEYS, set())
            self.assertTrue(config["device"]["identifiers"])
            self.assertEqual(config["availability_topic"], "nvr/status")
            for key in ("topic", "state_topic", "json_attributes_topic"):
                if key in config:
                    self.assertFalse(re.search(r"[#+]", config[key]), f"{key} must be a plain topic")
            if component == "binary_sensor":
                self.assertIn(config["device_class"], BINARY_SENSOR_CLASSES)
                self.assertEqual({config["payload_on"], config["payload_off"]}, {"ON", "OFF"})
        self.assertEqual(sorted(components), ["binary_sensor"] * 4 + ["camera"] * 4 + ["sensor"])  # Each of the four cameras, and the open-alarm sensor
        self.assertEqual(len({config["unique_id"] for config in configs.values()}), len(configs))  # Unique, so Home Assistant keeps them across restarts
        self.assertEqual(retained["nvr/status"], "online")
        camera = configs[f"homeassistant/camera/nvr/{self.d1}/config"]
        self.assertEqual((camera["topic"], camera["device"]["name"], camera["device"]["via_device"]), (f"nvr/{self.d1}/snapshot", "Dock 1", "nvr_hub"))
        self.assertTrue(all(message["retain"] for message in broker.messages if message["topic"].endswith("/config")))  # Retained: Home Assistant finds them when it starts
        self.assertEqual(broker.connections[0]["username"], "ha")

    def test_alarm_state_and_a_snapshot_are_published_and_cleared(self):
        broker = self.broker()
        self.enable(broker)
        alarm = self.add_alarm(self.d1)
        with patch("app.push.thumbnail", lambda alarm_id: b"\xff\xd8JPEG-" + alarm_id.encode()):
            manager = self.sync()
        retained = self.retained(broker)
        self.assertEqual((retained[f"nvr/{self.d1}/alarm"], retained[f"nvr/{self.d2}/alarm"], retained["nvr/alarms/open"]), ("ON", "OFF", "1"))
        attributes = json.loads(retained[f"nvr/{self.d1}/alarm/attributes"])
        self.assertEqual((attributes["alarm_id"], attributes["open_alarms"], attributes["rule_name"]), (alarm, 1, "Gate"))
        self.assertTrue(retained[f"nvr/{self.d1}/snapshot"].startswith("\xff\xd8JPEG-"))
        published = len(broker.messages)
        self.sync(manager)
        self.assertEqual(len(broker.messages), published)  # Nothing changed: nothing is sent again
        with connect() as db:
            db.execute("UPDATE alarms SET state='resolved' WHERE id=?", (alarm,))
        self.sync(manager)
        retained = self.retained(broker)
        self.assertEqual((retained[f"nvr/{self.d1}/alarm"], retained["nvr/alarms/open"]), ("OFF", "0"))

    def test_a_new_camera_is_announced_and_a_new_broker_gets_everything(self):
        broker = self.broker()
        self.enable(broker)
        manager = self.sync()
        added = self.add_camera("Gate 9")
        self.sync(manager)
        self.assertIn(f"homeassistant/camera/nvr/{added}/config", self.retained(broker))
        other = self.broker()
        self.enable(other, node_id="site2", discovery_prefix="ha/prefix")
        self.sync(manager)
        self.assertIn(f"ha/prefix/camera/site2/{added}/config", self.retained(other))

    def test_nothing_is_published_while_it_is_off_or_misconfigured(self):
        broker = self.broker()
        self.assertEqual(asyncio.run(home_assistant.HomeAssistantManager().sync()), 0)
        for body in ({"enabled": True}, {"url": "http://x", "enabled": True}, {"discovery_prefix": "bad prefix", "url": "mqtt://8.8.8.8:1883"}, {"node_id": "a/b"}, {"events_topic": "x/#"}):
            self.assertEqual(self.admin.put("/api/home-assistant", json=body).status_code, 422, body)
        self.assertEqual(broker.messages, [])

    def test_events_from_home_assistant_are_taken_in_and_mapped_like_any_others(self):
        broker = self.broker(username="ha", password="pw")
        settings = self.enable(broker)
        self.assertEqual((settings["broker"], settings["events_topic"]), (f"mqtt://127.0.0.1:{broker.port}", "nvr/events"))
        self.assertNotIn("pw", json.dumps(settings))
        sources = {item["id"]: item for item in self.admin.get("/api/event-sources").json()["sources"]}
        self.assertEqual(sources["home-assistant"]["kind"], "home_assistant")
        self.admin.put("/api/event-sources/home-assistant/mappings", json=[{"subject": "binary_sensor.front_door", "camera_ids": [self.d1]}])
        self.admin.post("/api/rules", json={"name": "HA door", "camera_ids": [self.d1], "condition": {"type": "external_event", "source_ids": ["home-assistant"]}})
        with patch("app.rule_actions._check_broker", lambda url, allow_private, delivering=False: mqtt.parse_broker(url)):
            self.run_manager(broker, [("nvr/events", json.dumps({"type": "door_forced", "subject": "binary_sensor.front_door", "message": "Front door opened"}).encode()),
                                      ("nvr/events/garage", json.dumps({"type": "sensor", "subject": "binary_sensor.garage"}).encode())])
        timeline = self.timeline(self.d1)
        self.assertEqual([(item["type"], item["subject"], item["source_name"]) for item in timeline], [("door_forced", "binary_sensor.front_door", "Home Assistant")])
        self.assertEqual(len(self.admin.get("/api/alarms", params={"state": "open"}).json()["alarms"]), 1)
        self.assertEqual(len(self.admin.get("/api/event-sources/home-assistant/events").json()["events"]), 2)  # The unmapped one is kept for writing a mapping

    def test_the_home_assistant_source_cannot_be_edited_or_deleted_as_a_source(self):
        self.enable(self.broker())
        self.assertEqual(self.admin.delete("/api/event-sources/home-assistant").status_code, 404)
        self.assertEqual(self.admin.put("/api/event-sources/home-assistant", json={"name": "x", "kind": "http", "secret": SECRET}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
