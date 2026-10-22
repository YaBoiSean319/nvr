from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

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
