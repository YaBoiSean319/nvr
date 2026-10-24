from __future__ import annotations

import os
import shutil
import unittest
from datetime import timedelta
from pathlib import Path

from app.rules import RulesManager
from app.store import connect
from tests.fixtures import detection_scene as scene
from tests.scene_case import SceneTestCase, parse

BOTTOM_LEFT = {"x": 0.0, "y": 0.6, "width": 0.3, "height": 0.4}
SCENE_LENGTH = scene.FRAME_INTERVAL * scene.FRAME_COUNT


class AlarmTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        rule = self.client.post("/api/rules", json={
            "name": "Gate entry", "enabled": True, "camera_ids": [self.camera_id], "group_ids": [], "classes": ["vehicle"],
            "condition": {"type": "zone_entry", "zone": BOTTOM_LEFT}, "schedule": None, "cooldown_seconds": 0,
        })
        self.assertEqual(rule.status_code, 201, rule.text)
        self.rule_id = rule.json()["id"]

    def record_scene_footage(self) -> int:
        """Index the fixture footage as the camera's recording of the scene."""
        relative = f"{self.camera_id}/scene.mp4"
        target = Path(os.environ["NVR_RECORDINGS_PATH"]) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.footage, target)
        with connect() as db:
            return db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                              (self.camera_id, relative, self.scene_start.isoformat(), (self.scene_start + SCENE_LENGTH).isoformat(),
                               target.stat().st_size)).lastrowid

    def alarms(self, **params) -> list[dict]:
        response = self.client.get("/api/alarms", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["alarms"]

    def test_a_rule_event_creates_one_alarm_with_a_playable_snapshot_and_clip(self):
        self.analyse_footage(companions=[RulesManager(poll_seconds=0.05)])
        self.record_scene_footage()

        alarms = self.alarms()
        self.assertEqual(len(alarms), 1, alarms)
        alarm = alarms[0]
        self.assertEqual((alarm["state"], alarm["rule_name"], alarm["camera_name"], alarm["label"]), ("new", "Gate entry", self.camera_name, "vehicle"))
        # The vehicle's centre enters the gate zone at frame 27.
        self.assertEqual(parse(alarm["occurred_at"]), scene.frame_times(self.scene_start)[27])

        detail = self.client.get(f"/api/alarms/{alarm['id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        body = detail.json()
        snapshot = self.client.get(body["snapshot_url"])
        self.assertEqual(snapshot.status_code, 200, snapshot.text[:200])
        self.assertEqual(snapshot.headers["content-type"], "image/jpeg")
        self.assertTrue(body["clip"]["segments"], body["clip"])
        self.assertLessEqual(parse(body["clip"]["starts_at"]), parse(alarm["occurred_at"]) - timedelta(seconds=5))
        clip = self.client.get(body["clip"]["segments"][0]["media_url"])
        self.assertEqual(clip.status_code, 200)
        self.assertEqual(clip.headers["content-type"], "video/mp4")
        self.assertEqual([entry["action"] for entry in body["history"]], ["created"])

    def operator_client(self):
        from fastapi.testclient import TestClient
        from app.security import hash_password
        from tests.test_support import TEST_PASSWORD

        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", ("watcher", hash_password(TEST_PASSWORD), "operator"))
            watcher_id = db.execute("SELECT id FROM users WHERE username='watcher'").fetchone()[0]
        operator = TestClient(self.client.app)
        operator.__enter__()
        self.addCleanup(operator.__exit__, None, None, None)
        self.assertEqual(operator.post("/api/auth/login", json={"username": "watcher", "password": TEST_PASSWORD}).status_code, 200)
        return operator, watcher_id

    def test_operators_handle_alarms_every_step_is_audited_and_only_administrators_delete(self):
        self.analyse_footage(companions=[RulesManager(poll_seconds=0.05)])
        operator, watcher_id = self.operator_client()
        alarm_id = self.alarms()[0]["id"]

        acknowledged = operator.post(f"/api/alarms/{alarm_id}/acknowledge")
        self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
        self.assertEqual((acknowledged.json()["state"], acknowledged.json()["acknowledged_by"]), ("acknowledged", "watcher"))
        self.assertEqual(operator.post(f"/api/alarms/{alarm_id}/acknowledge").status_code, 409)
        # Another signed-in person sees the change.
        self.assertEqual(self.client.get(f"/api/alarms/{alarm_id}").json()["state"], "acknowledged")

        assigned = self.client.post(f"/api/alarms/{alarm_id}/assign", json={"user_id": watcher_id})
        self.assertEqual((assigned.json()["state"], assigned.json()["assigned_to"]), ("assigned", "watcher"))
        commented = operator.post(f"/api/alarms/{alarm_id}/comments", json={"body": "  Delivery van at the gate  "})
        self.assertEqual(commented.status_code, 201, commented.text)
        self.assertEqual([(item["author"], item["body"]) for item in commented.json()["comments"]], [("watcher", "Delivery van at the gate")])
        resolved = operator.post(f"/api/alarms/{alarm_id}/resolve", json={"note": "Expected delivery"})
        self.assertEqual(resolved.json()["state"], "resolved")
        self.assertEqual(resolved.json()["resolved_by"], "watcher")
        self.assertEqual([entry["action"] for entry in resolved.json()["history"]], ["created", "acknowledged", "assigned", "commented", "resolved"])
        self.assertEqual(operator.post(f"/api/alarms/{alarm_id}/assign", json={"user_id": watcher_id}).status_code, 409)
        self.assertEqual(self.alarms(), [])
        self.assertEqual(len(self.alarms(state="resolved")), 1)

        with connect() as db:
            audited = [row[0] for row in db.execute("SELECT action FROM audit_log WHERE resource_type='alarm' AND resource_id=? ORDER BY id", (alarm_id,))]
        self.assertEqual(audited, ["alarm_created", "alarm_acknowledged", "alarm_assigned", "alarm_commented", "alarm_resolved"])

        self.assertEqual(operator.delete(f"/api/alarms/{alarm_id}").status_code, 403)
        self.assertEqual(self.client.delete(f"/api/alarms/{alarm_id}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/alarms/{alarm_id}").status_code, 404)

    def test_alarm_webhooks_are_queued_like_existing_events(self):
        with connect() as db:
            db.execute("INSERT INTO webhook_endpoints(id,name,target_url_ciphertext,signing_secret_ciphertext,events_json,enabled,created_at,updated_at) "
                       "VALUES('w','Receiver',X'00',X'00',?,1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
                       ('["alarm.created","alarm.resolved","ai.detection"]',))
        types = self.client.get("/api/webhooks/event-types").json()["event_types"]
        self.assertTrue({"alarm.created", "alarm.acknowledged", "alarm.assigned", "alarm.resolved", "ai.detection"} <= set(types))
        self.analyse_footage(companions=[RulesManager(poll_seconds=0.05)])
        alarm_id = self.alarms()[0]["id"]
        self.client.post(f"/api/alarms/{alarm_id}/resolve", json={})

        import json
        with connect() as db:
            rows = db.execute("SELECT event_type,event_id,payload_json FROM webhook_deliveries ORDER BY created_at").fetchall()
        alarm_events = [(row["event_type"], json.loads(row["payload_json"])) for row in rows if row["event_type"].startswith("alarm.")]
        self.assertEqual([kind for kind, _ in alarm_events], ["alarm.created", "alarm.resolved"])
        created, resolved = alarm_events[0][1], alarm_events[1][1]
        self.assertEqual((created["alarm_id"], created["state"], created["rule_name"], created["label"]), (alarm_id, "new", "Gate entry", "vehicle"))
        self.assertEqual((resolved["state"], resolved["actor"]), ("resolved", "keeper"))
        self.assertNotEqual(created["event_id"], resolved["event_id"])
        # Existing event payloads keep their shape.
        detection = next(json.loads(row["payload_json"]) for row in rows if row["event_type"] == "ai.detection")
        self.assertEqual(set(detection), {"event_id", "event_type", "occurred_at", "observation_id", "camera_id", "label", "confidence", "box",
                                          "model_id", "model_version", "provider"})

    def test_alarm_footage_outlives_retention_until_resolved_plus_the_configured_days(self):
        from app.retention import cleanup_expired_recordings

        self.analyse_footage(companions=[RulesManager(poll_seconds=0.05)])
        self.record_scene_footage()
        alarm_id = self.alarms()[0]["id"]
        with connect() as db:
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,1)", (self.camera_id,))
        self.assertEqual(self.client.put("/api/alarm-settings", json={"resolved_footage_days": 7}).json(), {"resolved_footage_days": 7})
        later = self.scene_start + timedelta(days=3)

        self.assertEqual(cleanup_expired_recordings(later)["removed_segments"], 0)  # open alarm
        self.client.post(f"/api/alarms/{alarm_id}/resolve", json={})
        from datetime import datetime, timezone
        resolved_at = datetime.now(timezone.utc)
        self.assertEqual(cleanup_expired_recordings(resolved_at + timedelta(days=6))["removed_segments"], 0)
        self.assertEqual(cleanup_expired_recordings(resolved_at + timedelta(days=8))["removed_segments"], 1)


if __name__ == "__main__":
    unittest.main()
