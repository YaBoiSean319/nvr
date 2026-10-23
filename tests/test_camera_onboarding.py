from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.security import hash_password
from app.store import initialize
from tests.test_support import TEST_PASSWORD


class CameraOnboardingAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in (
            "NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_COOKIE_SECURE",
        )}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.temporary.name) / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.temporary.name) / "credentials.key")
        os.environ["NVR_COOKIE_SECURE"] = "false"
        initialize()
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.client.post("/api/setup/admin", json={"username": "onboarding-admin", "password": TEST_PASSWORD})
        with patch("app.main.probe_stream", new=AsyncMock(return_value={
            "reachable": True, "video": {"codec": "h264", "width": 640, "height": 360},
        })), patch.object(self.context.app.state.recorders, "sync_camera", new=AsyncMock()):
            created = self.client.post("/api/cameras", json={
                "name": "Fixture Camera", "stream_url": "rtsp://192.0.2.24/stream",
            })
        if created.status_code != 201:
            self.fail(created.text)
        self.camera_id = created.json()["id"]
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute(
                "INSERT INTO users(username,password_hash,role) VALUES (?,?,?)",
                ("onboarding-operator", hash_password(TEST_PASSWORD), "operator"),
            )
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "onboarding-operator", "password": TEST_PASSWORD})

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temporary.cleanup()
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_operators_cannot_use_manual_or_csv_camera_onboarding(self):
        self.assertEqual(self.client.post("/api/cameras/onvif-profiles", json={"camera_ip": "198.51.100.25"}).status_code, 403)
        self.assertEqual(self.client.post("/api/cameras", json={"name": "Unauthorized", "stream_url": "rtsp://198.51.100.25/stream"}).status_code, 403)
        csv_text = "name,stream_url\nUnauthorized,rtsp://198.51.100.25/stream"
        self.assertEqual(self.client.post("/api/cameras/import/preview", json={"csv_text": csv_text}).status_code, 403)
        self.assertEqual(self.client.post("/api/cameras/import/apply", json={"csv_text": csv_text}).status_code, 403)

    def test_operators_can_read_inventory_but_cannot_change_camera_configuration(self):
        inventory = self.client.get("/api/cameras")
        self.assertEqual(inventory.status_code, 200, inventory.text)
        self.assertEqual([camera["id"] for camera in inventory.json()["cameras"]], [self.camera_id])
        self.assertEqual(self.client.patch(f"/api/cameras/{self.camera_id}", json={"name": "Changed"}).status_code, 403)
