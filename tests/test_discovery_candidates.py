from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app.discovery import DiscoveredDevice, DiscoveryResult
from app.main import app
from app.onvif_stream import CameraStreamSource
from app.store import connect
from tests.test_support import TEST_CAMERA_PASSWORD, TEST_PASSWORD


class DiscoveryCandidateTests(unittest.TestCase):
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
        self.client.post("/api/setup/admin", json={"username": "discovery-admin", "password": TEST_PASSWORD})

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temporary.cleanup()
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def scan(self, device_uuid="11111111-1111-4111-8111-111111111111", address="192.0.2.21"):
        device = DiscoveredDevice(
            device_uuid,
            (f"http://{address}/onvif/device_service",),
            ("onvif://www.onvif.org/name/Test%20Camera",),
        )
        with patch("app.main.discover_onvif", new=AsyncMock(return_value=DiscoveryResult((device,), 0, 1))):
            response = self.client.post("/api/discovery/scan", json={"timeout_seconds": 0.5})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["candidates"][0]

    def add_payload(self, address="192.0.2.21"):
        return {
            "name": "Test Camera",
            "camera_ip": address,
            "camera_username": "fixture-user",
            "camera_password": TEST_CAMERA_PASSWORD,
            "camera_brand": "axis",
            "onvif_profile_token": "main",
            "device_service_url": f"http://{address}/onvif/device_service",
            "motion_username": "fixture-user",
            "motion_password": TEST_CAMERA_PASSWORD,
            "recording_mode": "motion",
            "recording_enabled": False,
        }

    def test_linking_identity_keeps_existing_camera_and_history(self):
        good = {"reachable": True, "video": {"codec": "h264", "width": 1280, "height": 720}}
        with patch("app.main.probe_stream", new=AsyncMock(return_value=good)):
            existing = self.client.post("/api/cameras", json={"name": "Existing Camera", "stream_url": "rtsp://192.0.2.41/main"})
        self.assertEqual(existing.status_code, 201, existing.text)
        camera_id = existing.json()["id"]
        with connect() as db:
            db.execute(
                "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                (camera_id, "fixture/existing-camera.mp4", "2026-01-01T00:00:00Z", "2026-01-01T00:00:10Z", 1024),
            )

        candidate = self.scan()
        linked = self.client.post(
            f"/api/discovery/candidates/{candidate['id']}/link",
            json={"camera_id": camera_id},
        )

        self.assertEqual(linked.status_code, 200, linked.text)
        self.assertEqual(linked.json()["camera"]["id"], camera_id)
        self.assertEqual(self.client.get("/api/cameras").json()["cameras"][0]["id"], camera_id)
        with connect() as db:
            candidate_row = db.execute("SELECT linked_camera_id FROM discovery_candidates WHERE id=?", (candidate["id"],)).fetchone()
            segment = db.execute("SELECT camera_id,relative_path FROM recording_segments WHERE camera_id=?", (camera_id,)).fetchone()
        self.assertEqual(candidate_row["linked_camera_id"], camera_id)
        self.assertEqual(segment["camera_id"], camera_id)
        self.assertEqual(segment["relative_path"], "fixture/existing-camera.mp4")

    def test_failed_add_can_be_retried_and_keeps_safe_recording_defaults(self):
        candidate = self.scan()
        source = CameraStreamSource("rtsp://192.0.2.21:554/main", "http://192.0.2.21/onvif/device_service")
        with patch("app.main.resolve_camera_stream", new=AsyncMock(return_value=source)), patch(
            "app.main.probe_stream",
            new=AsyncMock(side_effect=[
                {"reachable": False, "reason": "Fixture stream unavailable"},
                {"reachable": True, "video": {"codec": "h264", "width": 1280, "height": 720}},
            ]),
        ):
            failed = self.client.post(f"/api/discovery/candidates/{candidate['id']}/add", json=self.add_payload())
            self.assertEqual(failed.status_code, 422)
            self.assertNotIn(TEST_CAMERA_PASSWORD, failed.text)
            still_unlinked = self.client.get("/api/discovery/candidates").json()["candidates"][0]
            self.assertIsNone(still_unlinked["linked_camera_id"])
            self.assertEqual(still_unlinked["capabilities"]["video_stream"], "unavailable")

            added = self.client.post(f"/api/discovery/candidates/{candidate['id']}/add", json=self.add_payload())

        self.assertEqual(added.status_code, 201, added.text)
        self.assertFalse(added.json()["recording_enabled"])
        self.assertEqual(added.json()["recording_mode"], "motion")
        self.assertNotIn(TEST_CAMERA_PASSWORD, added.text)
        listed = self.client.get("/api/discovery/candidates").json()["candidates"][0]
        self.assertEqual(listed["linked_camera_id"], added.json()["id"])
        self.assertEqual(listed["capabilities"]["video_stream"], "supported")

    def test_concurrent_adds_for_address_only_identity_create_at_most_one_camera(self):
        candidate = self.scan(
            "ipscan:5c0d37bca9d2262a49f4640a3a921dbe",
            "192.0.2.51",
        )
        source = CameraStreamSource("rtsp://192.0.2.51:554/main", "http://192.0.2.51/onvif/device_service")

        async def slow_resolve(*args, **kwargs):
            await asyncio.sleep(0.05)
            return source

        async def slow_probe(*args, **kwargs):
            await asyncio.sleep(0.05)
            return {"reachable": True, "video": {"codec": "h264", "width": 1280, "height": 720}}

        async def submit_twice():
            transport = httpx.ASGITransport(app=app)
            cookies = dict(self.client.cookies)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies=cookies) as first, httpx.AsyncClient(
                transport=transport, base_url="http://testserver", cookies=cookies
            ) as second:
                first_payload = self.add_payload("192.0.2.51")
                first_payload["name"] = "Address Camera One"
                second_payload = self.add_payload("192.0.2.51")
                second_payload["name"] = "Address Camera Two"
                return await asyncio.gather(
                    first.post(f"/api/discovery/candidates/{candidate['id']}/add", json=first_payload),
                    second.post(f"/api/discovery/candidates/{candidate['id']}/add", json=second_payload),
                )

        with patch("app.main.resolve_camera_stream", new=AsyncMock(side_effect=slow_resolve)), patch(
            "app.main.probe_stream", new=AsyncMock(side_effect=slow_probe)
        ):
            responses = asyncio.run(submit_twice())

        self.assertEqual(sorted(response.status_code for response in responses), [201, 409])
        self.assertEqual(len(self.client.get("/api/cameras").json()["cameras"]), 1)
        listed = self.client.get("/api/discovery/candidates").json()["candidates"][0]
        self.assertEqual(listed["linked_camera_id"], self.client.get("/api/cameras").json()["cameras"][0]["id"])
        self.assertEqual(listed["capabilities"]["video_stream"], "supported")
