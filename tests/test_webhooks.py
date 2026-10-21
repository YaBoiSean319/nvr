from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import administrator, app, current_user
from app.recording import RecordingManager
from app.store import connect, initialize


class WebhookApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_STORAGE_VOLUMES", "NVR_CREDENTIAL_KEY_PATH")}
        self.root = Path(self.temporary.name) / "recordings"
        self.root.mkdir()
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root)
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.temporary.name) / "credentials.key")
        os.environ["NVR_STORAGE_VOLUMES"] = json.dumps({"default": ""})
        initialize()
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(2,'operator','unused','operator')")
        self.active_user = {"id": 1, "username": "admin", "role": "administrator"}
        app.dependency_overrides[current_user] = lambda: self.active_user

        def require_admin():
            if self.active_user["role"] != "administrator":
                raise HTTPException(status_code=403, detail="administrator role required")
            return self.active_user

        app.dependency_overrides[administrator] = require_admin
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        app.dependency_overrides.pop(current_user, None)
        app.dependency_overrides.pop(administrator, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def _create_private_endpoint(self):
        return self.client.post("/api/webhooks", json={
            "name": "Operations receiver",
            "url": "http://10.23.4.5:8090/events/private-token",
            "events": ["recording.failure", "recording.uncertain", "recording.recovered", "storage.failure", "storage.recovered"],
            "allow_private_receiver": True,
        })

    def test_private_receiver_requires_explicit_authorization_and_configuration_redacts_secrets(self):
        denied = self.client.post("/api/webhooks", json={
            "name": "Private receiver", "url": "http://10.23.4.5/secret-path", "events": ["recording.failure"],
        })
        self.assertEqual(denied.status_code, 422)
        loopback = self.client.post("/api/webhooks", json={
            "name": "Loopback receiver", "url": "http://127.0.0.1/hook", "events": ["recording.failure"],
            "allow_private_receiver": True,
        })
        self.assertEqual(loopback.status_code, 422)

        created = self._create_private_endpoint()
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        secret = body["signing_secret"]
        endpoint = body["webhook"]
        self.assertTrue(secret)
        self.assertEqual(endpoint["url_origin"], "http://10.23.4.5:8090")
        self.assertNotIn(secret, json.dumps(endpoint))
        self.assertNotIn("private-token", json.dumps(endpoint))
        listed = self.client.get("/api/webhooks").json()["webhooks"][0]
        self.assertNotIn(secret, json.dumps(listed))
        self.assertNotIn("private-token", json.dumps(listed))
        with connect() as db:
            stored = db.execute("SELECT target_url_ciphertext,signing_secret_ciphertext FROM webhook_endpoints WHERE id=?", (endpoint["id"],)).fetchone()
        self.assertNotIn(b"private-token", stored["target_url_ciphertext"])
        self.assertNotIn(secret.encode(), stored["signing_secret_ciphertext"])

        self.active_user = {"id": 2, "username": "operator", "role": "operator"}
        self.assertEqual(self.client.get("/api/webhooks").status_code, 403)
        self.assertEqual(self.client.get("/api/webhooks/deliveries").status_code, 403)

    def test_incident_delivery_is_signed_deduplicated_retried_and_shows_outcome(self):
        created = self._create_private_endpoint()
        self.assertEqual(created.status_code, 201, created.text)
        endpoint = created.json()["webhook"]
        secret = created.json()["signing_secret"]
        camera_id = str(uuid.uuid4())
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with connect() as db:
            db.execute(
                "INSERT INTO cameras(id,name,stream_url_ciphertext,recording_enabled,recording_mode,recording_status,last_video_at,last_footage_write_at) "
                "VALUES(?,?,X'01',1,'continuous','reconnecting',?,?)",
                (camera_id, "Webhook test camera", old, old),
            )

        real_client = httpx.AsyncClient
        requests = []

        def receiver(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(503 if len(requests) == 1 else 202, request=request)

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(receiver)
            return real_client(*args, **kwargs)

        with patch("app.webhooks.httpx.AsyncClient", side_effect=client_factory):
            manager = RecordingManager()
            manager._sync_coverage(camera_id)
            manager._sync_coverage(camera_id)
            deadline = time.monotonic() + 10
            deliveries = []
            while time.monotonic() < deadline:
                deliveries = self.client.get("/api/webhooks/deliveries").json()["deliveries"]
                if deliveries and deliveries[0]["status"] == "delivered":
                    break
                time.sleep(0.1)
        self.assertEqual(len(deliveries), 1, "repeated health observations must enqueue one stable incident event")
        delivery = deliveries[0]
        self.assertEqual(delivery["endpoint_id"], endpoint["id"])
        self.assertEqual(delivery["status"], "delivered")
        self.assertEqual(delivery["attempt_count"], 2)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].content, requests[1].content)
        self.assertEqual(requests[0].headers["X-NVR-Event-ID"], requests[1].headers["X-NVR-Event-ID"])
        self.assertEqual(requests[0].headers["X-NVR-Event-Type"], "recording.failure")
        self.assertEqual(json.loads(requests[0].content)["camera_id"], camera_id)
        for request in requests:
            stamp = request.headers["X-NVR-Timestamp"]
            expected = hmac.new(secret.encode(), stamp.encode() + b"." + request.content, hashlib.sha256).hexdigest()
            self.assertEqual(request.headers["X-NVR-Signature"], f"sha256={expected}")
        self.assertIsNotNone(delivery["delivered_at"])

        now = datetime.now(timezone.utc).isoformat()
        with connect() as db:
            db.execute("UPDATE cameras SET recording_status='recording',last_video_at=?,last_footage_write_at=? WHERE id=?", (now, now, camera_id))
        with patch("app.webhooks.httpx.AsyncClient", side_effect=client_factory):
            manager._sync_coverage(camera_id)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                deliveries = self.client.get("/api/webhooks/deliveries").json()["deliveries"]
                if len(deliveries) >= 2 and deliveries[0]["status"] == "delivered":
                    break
                time.sleep(0.05)
        self.assertEqual(len(deliveries), 2)
        self.assertGreaterEqual(len(requests), 3, f"recovery deliveries have not reached the receiver: {deliveries}")
        self.assertEqual(requests[2].headers["X-NVR-Event-Type"], "recording.recovered")

    def test_inflight_delivery_is_requeued_after_restart_and_test_event_is_audited(self):
        created = self._create_private_endpoint()
        self.assertEqual(created.status_code, 201, created.text)
        endpoint_id = created.json()["webhook"]["id"]
        self.context.portal.call(self.client.app.state.webhooks.stop)
        queued = self.client.post(f"/api/webhooks/{endpoint_id}/test")
        self.assertEqual(queued.status_code, 202, queued.text)
        event_id = queued.json()["event_id"]
        with connect() as db:
            db.execute("UPDATE webhook_deliveries SET status='delivering',attempt_count=1 WHERE event_id=?", (event_id,))

        self.context.__exit__(None, None, None)
        real_client = httpx.AsyncClient
        requests = []

        def receiver(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, request=request)

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(receiver)
            return real_client(*args, **kwargs)

        with patch("app.webhooks.httpx.AsyncClient", side_effect=client_factory):
            self.context = TestClient(app)
            self.client = self.context.__enter__()
            deadline = time.monotonic() + 5
            deliveries = []
            while time.monotonic() < deadline:
                deliveries = self.client.get("/api/webhooks/deliveries").json()["deliveries"]
                if deliveries and deliveries[0]["status"] == "delivered":
                    break
                time.sleep(0.05)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["X-NVR-Event-ID"], event_id)
        self.assertEqual(requests[0].headers["X-NVR-Event-Type"], "webhook.test")
        self.assertEqual(deliveries[0]["status"], "delivered")
        self.assertEqual(deliveries[0]["attempt_count"], 2)
        with connect() as db:
            audit = db.execute("SELECT actor_id,action FROM audit_log WHERE resource_type='webhook' ORDER BY id").fetchall()
        self.assertIn((1, "webhook_test_requested"), [(row["actor_id"], row["action"]) for row in audit])


if __name__ == "__main__":
    unittest.main()
