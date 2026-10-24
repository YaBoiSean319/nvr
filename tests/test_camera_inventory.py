from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from tests.test_support import patch_camera_probe, TEST_CAMERA_PASSWORD, TEST_PASSWORD


class CameraInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in (
            "NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_COOKIE_SECURE"
        )}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.temporary.name) / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.temporary.name) / "credentials.key")
        os.environ["NVR_COOKIE_SECURE"] = "false"
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.client.post("/api/setup/admin", json={"username": "inventory-admin", "password": TEST_PASSWORD})

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temporary.cleanup()
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_admin_can_edit_camera_name_without_exposing_stream_credentials(self):
        probe = {"reachable": True, "video": {"codec": "h264", "width": 640, "height": 360}}
        secret_url = f"rtsp://fixture-user:{TEST_CAMERA_PASSWORD}@camera.example.test/stream"
        with patch_camera_probe(new=AsyncMock(return_value=probe)):
            created = self.client.post("/api/cameras", json={"name": "Loading Dock", "stream_url": secret_url})
        self.assertEqual(created.status_code, 201, created.text)

        updated = self.client.patch(f"/api/cameras/{created.json()['id']}", json={"name": "North Loading Dock"})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["name"], "North Loading Dock")
        self.assertEqual(updated.json()["recording_status"], "stopped")

        inventory = self.client.get("/api/cameras")
        self.assertEqual(inventory.status_code, 200)
        self.assertEqual(inventory.json()["cameras"][0]["name"], "North Loading Dock")
        self.assertNotIn(TEST_CAMERA_PASSWORD, updated.text + inventory.text)

    def test_camera_name_edit_rejects_blank_or_duplicate_names_without_changing_inventory(self):
        probe = {"reachable": True, "video": {"codec": "h264", "width": 640, "height": 360}}
        with patch_camera_probe(new=AsyncMock(return_value=probe)):
            first = self.client.post("/api/cameras", json={"name": "North Entrance", "stream_url": "rtsp://127.0.0.1/one"})
            second = self.client.post("/api/cameras", json={"name": "South Entrance", "stream_url": "rtsp://127.0.0.1/two"})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)

        blank = self.client.patch(f"/api/cameras/{first.json()['id']}", json={"name": "  "})
        null_name = self.client.patch(f"/api/cameras/{first.json()['id']}", json={"name": None})
        duplicate = self.client.patch(f"/api/cameras/{first.json()['id']}", json={"name": "South Entrance"})
        self.assertEqual(blank.status_code, 422)
        self.assertEqual(null_name.status_code, 422)
        self.assertEqual(duplicate.status_code, 409)
        names = {camera["name"] for camera in self.client.get("/api/cameras").json()["cameras"]}
        self.assertEqual(names, {"North Entrance", "South Entrance"})

    def test_admin_can_replace_and_persist_tested_onvif_source_without_returning_credentials(self):
        from app.onvif_stream import CameraStreamSource

        high = {"reachable": True, "audio": False, "video": {"codec": "h264", "width": 1920, "height": 1080}}
        low = {"reachable": True, "audio": False, "video": {"codec": "h264", "width": 640, "height": 360}}
        with patch("app.api.cameras.resolve_camera_stream", new=AsyncMock(side_effect=[
            CameraStreamSource(f"rtsp://fixture-old-user:{TEST_CAMERA_PASSWORD}@198.51.100.10:554/main", "http://198.51.100.10/onvif/device_service"),
            CameraStreamSource(f"rtsp://fixture-old-user:{TEST_CAMERA_PASSWORD}@198.51.100.10:554/sub", "http://198.51.100.10/onvif/device_service"),
        ])), patch_camera_probe(new=AsyncMock(side_effect=[high, low])):
            created = self.client.post("/api/cameras", json={
                "name": "Lobby", "camera_ip": "198.51.100.10", "camera_username": "fixture-old-user", "camera_password": TEST_CAMERA_PASSWORD,
                "camera_brand": "axis", "onvif_profile_token": "main-profile", "live_substream_profile_token": "sub-profile",
                "device_service_url": "http://198.51.100.10/onvif/device_service", "recording_mode": "disabled",
            })
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json().get("camera_ip"), "198.51.100.10")
        self.assertEqual(created.json().get("camera_brand"), "axis")
        self.assertEqual(created.json().get("onvif_profile_token"), "main-profile")
        self.assertEqual(created.json().get("live_substream_profile_token"), "sub-profile")
        camera_id = created.json()["id"]

        with patch("app.api.cameras.resolve_camera_stream", new=AsyncMock(side_effect=[
            CameraStreamSource(f"rtsp://fixture-new-user:{TEST_CAMERA_PASSWORD}@198.51.100.11:554/main", "http://198.51.100.11:8899/custom/service"),
            CameraStreamSource(f"rtsp://fixture-new-user:{TEST_CAMERA_PASSWORD}@198.51.100.11:554/sub", "http://198.51.100.11:8899/custom/service"),
        ])) as resolve, patch_camera_probe(new=AsyncMock(side_effect=[high, low])):
            updated = self.client.put(f"/api/cameras/{camera_id}/source", json={
                "camera_ip": "198.51.100.11", "camera_username": "fixture-new-user", "camera_password": TEST_CAMERA_PASSWORD,
                "camera_brand": "hikvision", "onvif_profile_token": "replacement-main", "live_substream_profile_token": "replacement-sub",
                "device_service_url": "http://198.51.100.11:8899/custom/service",
            })
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["camera_ip"], "198.51.100.11")
        self.assertEqual(updated.json()["camera_brand"], "hikvision")
        self.assertEqual(updated.json()["device_service_url"], "http://198.51.100.11:8899/custom/service")
        self.assertEqual(updated.json()["onvif_profile_token"], "replacement-main")
        self.assertEqual(updated.json()["live_substream_profile_token"], "replacement-sub")
        self.assertTrue(updated.json()["live_substream_available"])
        self.assertEqual(resolve.await_args_list[0].args[:5], ("198.51.100.11", "fixture-new-user", TEST_CAMERA_PASSWORD, "http://198.51.100.11:8899/custom/service", "replacement-main"))

        inventory = self.client.get("/api/cameras")
        self.assertIn("rtsp://198.51.100.11:554", inventory.text)
        self.assertNotIn(TEST_CAMERA_PASSWORD, created.text + updated.text + inventory.text)
        self.assertNotIn("fixture-new-user", created.text + updated.text + inventory.text)
        self.assertEqual(inventory.json()["cameras"][0]["camera_ip"], "198.51.100.11")

        from app.onvif_stream import OnvifStreamProfile
        with patch("app.api.cameras.list_camera_stream_profiles", new=AsyncMock(return_value=(
            "http://198.51.100.11:8899/custom/service", (OnvifStreamProfile("replacement-main", "Main stream"),)
        ))) as profiles:
            loaded = self.client.post(f"/api/cameras/{camera_id}/source/profiles", json={
                "camera_ip": "198.51.100.11", "camera_brand": "hikvision",
                "device_service_url": "http://198.51.100.11:8899/custom/service",
            })
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json()["profiles"], [{"token": "replacement-main", "name": "Main stream"}])
        self.assertEqual(profiles.await_args.args[1:3], ("fixture-new-user", TEST_CAMERA_PASSWORD))
        self.assertNotIn(TEST_CAMERA_PASSWORD, loaded.text)

        with patch("app.api.cameras.resolve_camera_stream", new=AsyncMock(return_value=CameraStreamSource(
            f"rtsp://fixture-new-user:{TEST_CAMERA_PASSWORD}@198.51.100.11:554/main", "http://198.51.100.11:8899/custom/service"
        ))) as resolve, patch_camera_probe(new=AsyncMock(return_value=high)):
            tested = self.client.post(f"/api/cameras/{camera_id}/source/test", json={
                "camera_ip": "198.51.100.11", "camera_brand": "hikvision", "onvif_profile_token": "replacement-main",
                "device_service_url": "http://198.51.100.11:8899/custom/service",
            })
        self.assertEqual(tested.status_code, 200, tested.text)
        self.assertEqual(resolve.await_args.args[:5], ("198.51.100.11", "fixture-new-user", TEST_CAMERA_PASSWORD, "http://198.51.100.11:8899/custom/service", "replacement-main"))
        self.assertNotIn(TEST_CAMERA_PASSWORD, tested.text)
        from app.store import initialize
        initialize()
        persisted = self.client.get("/api/cameras").json()["cameras"][0]
        self.assertEqual(persisted["camera_ip"], "198.51.100.11")
        self.assertEqual(persisted["onvif_profile_token"], "replacement-main")
        self.assertEqual(persisted["device_service_url"], "http://198.51.100.11:8899/custom/service")

    def test_failed_source_probe_keeps_the_saved_camera_source_unchanged(self):
        from app.onvif_stream import CameraStreamSource

        good = {"reachable": True, "video": {"codec": "h264", "width": 1280, "height": 720}}
        with patch("app.api.cameras.resolve_camera_stream", new=AsyncMock(return_value=CameraStreamSource(
            f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@198.51.100.20:554/main", "http://198.51.100.20/onvif/device_service"
        ))), patch_camera_probe(new=AsyncMock(return_value=good)):
            created = self.client.post("/api/cameras", json={
                "name": "Gate", "camera_ip": "198.51.100.20", "camera_username": "fixture-user", "camera_password": TEST_CAMERA_PASSWORD,
                "onvif_profile_token": "main", "recording_mode": "disabled",
            })
        self.assertEqual(created.status_code, 201, created.text)

        with patch("app.api.cameras.resolve_camera_stream", new=AsyncMock(return_value=CameraStreamSource(
            f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@198.51.100.21:554/main", "http://198.51.100.21/onvif/device_service"
        ))), patch_camera_probe(new=AsyncMock(return_value={"reachable": False, "reason": "Stream unavailable"})):
            rejected = self.client.put(f"/api/cameras/{created.json()['id']}/source", json={
                "camera_ip": "198.51.100.21", "camera_username": "fixture-user", "camera_password": TEST_CAMERA_PASSWORD,
                "onvif_profile_token": "replacement", "device_service_url": "http://198.51.100.21/onvif/device_service",
            })
        self.assertEqual(rejected.status_code, 422)
        self.assertNotIn(TEST_CAMERA_PASSWORD, rejected.text)
        saved = self.client.get("/api/cameras").json()["cameras"][0]
        self.assertEqual(saved["camera_ip"], "198.51.100.20")
        self.assertEqual(saved["onvif_profile_token"], "main")
        self.assertEqual(saved["stream_url"], "rtsp://198.51.100.20:554")

    def test_source_edit_can_change_username_and_keep_saved_password(self):
        from app.onvif_stream import CameraStreamSource

        capability = {"reachable": True, "video": {"codec": "h264", "width": 1280, "height": 720}}
        with patch("app.api.cameras.resolve_camera_stream", new=AsyncMock(return_value=CameraStreamSource(
            f"rtsp://fixture-old-user:{TEST_CAMERA_PASSWORD}@198.51.100.40:554/main", "http://198.51.100.40/onvif/device_service"
        ))), patch_camera_probe(new=AsyncMock(return_value=capability)):
            created = self.client.post("/api/cameras", json={
                "name": "Side Entrance", "camera_ip": "198.51.100.40", "camera_username": "fixture-old-user",
                "camera_password": TEST_CAMERA_PASSWORD, "onvif_profile_token": "main", "recording_mode": "disabled",
            })
        self.assertEqual(created.status_code, 201, created.text)

        with patch("app.api.cameras.resolve_camera_stream", new=AsyncMock(return_value=CameraStreamSource(
            f"rtsp://renamed:{TEST_CAMERA_PASSWORD}@198.51.100.40:554/main", "http://198.51.100.40/onvif/device_service"
        ))) as resolve, patch_camera_probe(new=AsyncMock(return_value=capability)):
            updated = self.client.put(f"/api/cameras/{created.json()['id']}/source", json={
                "camera_ip": "198.51.100.40", "camera_username": "renamed", "onvif_profile_token": "main",
            })

        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(resolve.await_args.args[:3], ("198.51.100.40", "renamed", TEST_CAMERA_PASSWORD))
        self.assertNotIn(TEST_CAMERA_PASSWORD, updated.text)
        self.assertNotIn("renamed", updated.text)

        with patch("app.api.cameras.resolve_camera_stream", new=AsyncMock(return_value=CameraStreamSource(
            f"rtsp://renamed:{TEST_CAMERA_PASSWORD}@198.51.100.40:554/main", "http://198.51.100.40/onvif/device_service"
        ))) as resolve, patch_camera_probe(new=AsyncMock(return_value=capability)):
            tested = self.client.post(f"/api/cameras/{created.json()['id']}/source/test", json={
                "camera_ip": "198.51.100.40", "onvif_profile_token": "main",
            })
        self.assertEqual(tested.status_code, 200, tested.text)
        self.assertEqual(resolve.await_args.args[:3], ("198.51.100.40", "renamed", TEST_CAMERA_PASSWORD))
        self.assertNotIn(TEST_CAMERA_PASSWORD, tested.text)

    def test_only_administrators_can_load_or_test_camera_source_configuration(self):
        from app.security import hash_password
        from app.store import connect

        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", ("operator-user", hash_password(TEST_PASSWORD), "operator"))
        login = self.client.post("/api/auth/login", json={"username": "operator-user", "password": TEST_PASSWORD})
        self.assertEqual(login.status_code, 200)
        payload = {"camera_ip": "198.51.100.30", "onvif_profile_token": "main", "camera_brand": "axis"}
        self.assertEqual(self.client.post("/api/cameras/not-a-camera/source/profiles", json=payload).status_code, 403)
        self.assertEqual(self.client.post("/api/cameras/not-a-camera/source/test", json=payload).status_code, 403)
        self.assertEqual(self.client.put("/api/cameras/not-a-camera/source", json=payload).status_code, 403)


if __name__ == "__main__":
    unittest.main()
