"""The check of a camera's lower-resolution live stream, the same when a camera is added and when its source is changed (#387).

The stream chosen for grid viewing must resolve over ONVIF, open, and be smaller than the main stream. Adding a camera and changing a
camera's source each wrote this check out; these tests pin what each answers, through the API, before the two become one.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.onvif_stream import CameraStreamSource
from tests.test_support import TEST_CAMERA_PASSWORD, TEST_PASSWORD, patch_camera_probe

SERVICE = "http://198.51.100.10/onvif/device_service"
MAIN = CameraStreamSource(f"rtsp://fixture-user:{TEST_CAMERA_PASSWORD}@198.51.100.10:554/main", SERVICE)
SUB = CameraStreamSource(f"rtsp://fixture-user:{TEST_CAMERA_PASSWORD}@198.51.100.10:554/sub", SERVICE)
HIGH = {"reachable": True, "audio": False, "video": {"codec": "h264", "width": 1920, "height": 1080}}
LOW = {"reachable": True, "audio": False, "video": {"codec": "h264", "width": 640, "height": 360}}
UNREACHABLE = {"reachable": False, "reason": "connection refused"}
SOURCE = {"camera_ip": "198.51.100.10", "camera_username": "fixture-user", "camera_password": TEST_CAMERA_PASSWORD, "camera_brand": "axis",
          "onvif_profile_token": "main-profile", "live_substream_profile_token": "sub-profile", "device_service_url": SERVICE}


class LiveSubstreamCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_COOKIE_SECURE")}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.temporary.name) / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.temporary.name) / "credentials.key")
        os.environ["NVR_COOKIE_SECURE"] = "false"
        self.context = TestClient(app, raise_server_exceptions=False)  # A request that fails inside the server answers 500, as it would for a browser
        self.client = self.context.__enter__()
        self.client.post("/api/setup/admin", json={"username": "substream-admin", "password": TEST_PASSWORD})

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temporary.cleanup()
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def add(self, resolved, probes, **extra):
        with patch("app.api.cameras.resolve_camera_stream", new=AsyncMock(side_effect=resolved)), patch_camera_probe(new=AsyncMock(side_effect=probes)):
            return self.client.post("/api/cameras", json={"name": "Lobby", "recording_mode": "disabled", **SOURCE, **extra})

    def change(self, resolved, probes):
        with patch("app.api.cameras.resolve_camera_stream", new=AsyncMock(side_effect=[MAIN])), patch_camera_probe(new=AsyncMock(side_effect=[HIGH])):
            created = self.client.post("/api/cameras", json={"name": "Lobby", "recording_mode": "disabled", **{**SOURCE, "live_substream_profile_token": None}})
        self.assertEqual(created.status_code, 201, created.text)
        with patch("app.api.cameras.resolve_camera_stream", new=AsyncMock(side_effect=resolved)), patch_camera_probe(new=AsyncMock(side_effect=probes)):
            return self.client.put(f"/api/cameras/{created.json()['id']}/source", json=SOURCE)

    def both(self, resolved, probes):
        """The answer to adding a camera and to changing a camera's source, given the same camera."""
        added = self.add(list(resolved), list(probes))
        self.tearDown()  # A fresh site for the other path: the camera just added would otherwise be a duplicate of the one about to be changed
        self.setUp()
        return {"add": added, "change": self.change(list(resolved), list(probes))}

    def test_a_smaller_stream_that_opens_is_kept_for_grid_viewing(self):
        for path, response in self.both([MAIN, SUB], [HIGH, LOW]).items():
            with self.subTest(path):
                self.assertIn(response.status_code, (200, 201), response.text)
                self.assertTrue(response.json()["live_substream_available"])
                self.assertEqual(response.json()["live_substream_profile_token"], "sub-profile")

    def test_a_stream_that_does_not_resolve_is_refused(self):
        for path, response in self.both([MAIN, ValueError("no such profile")], [HIGH]).items():
            with self.subTest(path):
                self.assertEqual((response.status_code, response.json()["detail"]), (422, "The selected live substream could not be resolved"))

    def test_a_network_error_while_it_is_looked_up_is_refused_with_its_reason(self):
        # Adding a camera used to end with a server error (500) here, where changing a source gave this answer.
        for path, response in self.both([MAIN, OSError("connection reset")], [HIGH]).items():
            with self.subTest(path):
                self.assertEqual((response.status_code, response.json()["detail"]), (422, "The selected live substream could not be resolved"))

    def test_a_stream_that_does_not_open_is_refused(self):
        for path, response in self.both([MAIN, SUB], [HIGH, UNREACHABLE]).items():
            with self.subTest(path):
                self.assertEqual((response.status_code, response.json()["detail"]), (422, "The selected live substream failed its video test"))

    def test_a_stream_no_smaller_than_the_main_one_is_refused(self):
        for path, response in self.both([MAIN, SUB], [HIGH, HIGH]).items():
            with self.subTest(path):
                self.assertEqual((response.status_code, response.json()["detail"]), (422, "Choose an ONVIF profile with a lower video resolution for grid viewing"))

    def test_a_camera_added_anyway_uses_its_main_stream_when_the_smaller_one_fails(self):
        response = self.add([MAIN, SUB], [HIGH, UNREACHABLE], save_anyway=True)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertFalse(response.json()["live_substream_available"])

    def test_without_a_profile_for_grid_viewing_nothing_else_is_looked_up(self):
        added = self.add([MAIN], [HIGH], live_substream_profile_token=None)
        self.assertEqual(added.status_code, 201, added.text)
        self.assertFalse(added.json()["live_substream_available"])


if __name__ == "__main__":
    unittest.main()
