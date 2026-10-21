from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import administrator, app, current_user
from app.store import connect, initialize


class ExternalHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_STORAGE_VOLUMES", "NVR_CREDENTIAL_KEY_PATH")}
        root = Path(self.temporary.name) / "recordings"
        root.mkdir()
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root)
        os.environ["NVR_STORAGE_VOLUMES"] = json.dumps({"default": ""})
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.temporary.name) / "credentials.key")
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

    def test_signed_heartbeat_reports_last_success_without_claiming_checker_health(self):
        real_client = httpx.AsyncClient
        requests = []

        def receiver(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200 if len(requests) == 1 else 503, request=request)

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(receiver)
            return real_client(*args, **kwargs)

        with patch("app.heartbeat.httpx.AsyncClient", side_effect=client_factory), patch("app.heartbeat.validate_target_url", side_effect=lambda target, allow_private: target):
            configured = self.client.put("/api/heartbeat", json={
                "target_url": "https://receiver.example.invalid/nvr",
                "interval_seconds": 30,
                "allow_private_receiver": False,
            })
            self.assertEqual(configured.status_code, 200, configured.text)
            secret = configured.json()["signing_secret"]
            deadline = time.monotonic() + 5
            state = {}
            while time.monotonic() < deadline:
                state = self.client.get("/api/heartbeat").json()
                if state["last_success_at"]:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(state["last_success_at"])
            self.assertEqual(state["url_origin"], "https://receiver.example.invalid")
            self.assertIn("does not prove", state["checker_note"])

            queued = self.client.post("/api/heartbeat/test")
            self.assertEqual(queued.status_code, 202, queued.text)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = self.client.get("/api/heartbeat").json()
                if state["last_error"]:
                    break
                time.sleep(0.05)

        self.assertEqual(state["last_error"], "receiver_http_503")
        self.assertIsNotNone(state["last_success_at"], "failed sends must not erase the last successful send")
        self.assertEqual(state["last_http_status"], 503)
        self.assertEqual(len(requests), 2)
        self.assertFalse(json.loads(requests[0].content)["test"])
        self.assertTrue(json.loads(requests[1].content)["test"])
        for request in requests:
            stamp = request.headers["X-NVR-Timestamp"]
            expected = hmac.new(secret.encode(), stamp.encode() + b"." + request.content, hashlib.sha256).hexdigest()
            self.assertEqual(request.headers["X-NVR-Signature"], f"sha256={expected}")
        self.assertEqual(self.client.delete("/api/heartbeat").status_code, 204)
        self.assertFalse(self.client.get("/api/heartbeat").json()["enabled"])

    def test_interval_validation_and_admin_only(self):
        bad_interval = self.client.put("/api/heartbeat", json={
            "target_url": "https://receiver.example.invalid/nvr", "interval_seconds": 20,
        })
        self.assertEqual(bad_interval.status_code, 422)
        self.active_user = {"id": 2, "username": "operator", "role": "operator"}
        self.assertEqual(self.client.get("/api/heartbeat").status_code, 403)
        self.assertEqual(self.client.put("/api/heartbeat", json={
            "target_url": "https://receiver.example.invalid/nvr", "interval_seconds": 60,
        }).status_code, 403)


if __name__ == "__main__":
    unittest.main()
