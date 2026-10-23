import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.test_support import TEST_PASSWORD


class CameraBulkActionsApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.temporary.name) / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.temporary.name) / "credentials.key")
        os.environ["NVR_COOKIE_SECURE"] = "false"
        os.environ["NVR_SEGMENT_SECONDS"] = "10"
        for key in ("NVR_PUBLIC_URL", "NVR_OIDC_TENANT_ID", "NVR_OIDC_CLIENT_ID", "NVR_OIDC_CLIENT_SECRET", "NVR_OIDC_SESSION_SECRET", "NVR_OIDC_ADMIN_ROLES", "NVR_OIDC_OPERATOR_ROLES"):
            os.environ.pop(key, None)
        from app.main import app
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temporary.cleanup()
        for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_COOKIE_SECURE", "NVR_SEGMENT_SECONDS", "NVR_PUBLIC_URL", "NVR_OIDC_TENANT_ID", "NVR_OIDC_CLIENT_ID", "NVR_OIDC_CLIENT_SECRET", "NVR_OIDC_SESSION_SECRET", "NVR_OIDC_ADMIN_ROLES", "NVR_OIDC_OPERATOR_ROLES"):
            os.environ.pop(key, None)

    def add_camera(self, name, suffix, recording_mode="disabled"):
        capability = {"reachable": True, "video": {"codec": "h264", "width": 640, "height": 360}}
        with patch("app.main.probe_stream", new=AsyncMock(return_value=capability)):
            response = self.client.post("/api/cameras", json={
                "name": name,
                "stream_url": f"rtsp://127.0.0.1/{suffix}",
                "recording_mode": recording_mode,
            })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_bulk_update_reports_each_camera_and_does_not_mutate_archived_or_missing_targets(self):
        self.client.post("/api/setup/admin", json={"username": "local-admin", "password": TEST_PASSWORD})
        active = self.add_camera("Bulk active", "active", recording_mode="continuous")
        archived = self.add_camera("Bulk archived", "archived")
        self.client.delete(f"/api/cameras/{archived['id']}")
        missing_id = str(uuid.uuid4())
        with patch.object(self.context.app.state.recorders, "sync_camera", new=AsyncMock()):
            response = self.client.post("/api/cameras/bulk-update", json={
                "camera_ids": [active["id"], archived["id"], missing_id],
                "recording_enabled": True,
            })

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual([item["camera_id"] for item in result["results"]], [active["id"], archived["id"], missing_id])
        self.assertEqual([item["status"] for item in result["results"]], ["success", "failed", "failed"])
        self.assertEqual([item["code"] for item in result["results"]], [None, "archived", "missing"])
        self.assertEqual([camera["id"] for camera in result["cameras"]], [active["id"]])
        inventory = {camera["id"]: camera for camera in self.client.get("/api/cameras?include_archived=true").json()["cameras"]}
        self.assertTrue(inventory[active["id"]]["recording_enabled"])
        self.assertFalse(inventory[archived["id"]]["recording_enabled"])

    def test_bulk_update_keeps_per_camera_success_when_recorder_refresh_fails_after_commit(self):
        self.client.post("/api/setup/admin", json={"username": "local-admin", "password": TEST_PASSWORD})
        first = self.add_camera("Bulk first", "first", recording_mode="continuous")
        second = self.add_camera("Bulk second", "second", recording_mode="continuous")
        sync_camera = AsyncMock(side_effect=[RuntimeError("recorder unavailable"), None])
        with patch.object(self.context.app.state.recorders, "sync_camera", new=sync_camera):
            response = self.client.post("/api/cameras/bulk-update", json={
                "camera_ids": [first["id"], second["id"]],
                "recording_enabled": True,
            })

        self.assertEqual(response.status_code, 200, response.text)
        results = response.json()["results"]
        self.assertEqual([item["status"] for item in results], ["success", "success"])
        self.assertIn("recorder could not be refreshed", results[0]["warning"])
        self.assertNotIn("warning", results[1])
        self.assertEqual(len(response.json()["cameras"]), 2)
        inventory = {camera["id"]: camera for camera in self.client.get("/api/cameras").json()["cameras"]}
        self.assertTrue(inventory[first["id"]]["recording_enabled"])
        self.assertTrue(inventory[second["id"]]["recording_enabled"])

    def test_bulk_recording_enablement_preserves_disabled_recording_mode(self):
        self.client.post("/api/setup/admin", json={"username": "local-admin", "password": TEST_PASSWORD})
        camera = self.add_camera("Disabled bulk camera", "disabled")

        with patch.object(self.context.app.state.recorders, "sync_camera", new=AsyncMock()):
            response = self.client.post("/api/cameras/bulk-update", json={
                "camera_ids": [camera["id"]],
                "recording_enabled": True,
            })

        self.assertEqual(response.status_code, 200, response.text)
        outcome = response.json()["results"][0]
        self.assertEqual(outcome["status"], "success")
        self.assertIn("remains disabled", outcome["warning"])
        persisted = self.client.get("/api/cameras").json()["cameras"][0]
        self.assertEqual(persisted["recording_mode"], "disabled")
        self.assertFalse(persisted["recording_enabled"])

    def test_bulk_update_rejects_a_batch_above_the_existing_500_camera_limit(self):
        self.client.post("/api/setup/admin", json={"username": "local-admin", "password": TEST_PASSWORD})
        response = self.client.post("/api/cameras/bulk-update", json={
            "camera_ids": [str(uuid.uuid4()) for _ in range(501)],
            "group_name": "Operations",
        })
        self.assertEqual(response.status_code, 422, response.text)
