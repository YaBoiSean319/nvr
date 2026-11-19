"""Where a camera is on the network is shown to administrators only (#377).

Operators, read-scope API tokens and video walls get the camera list without its address, ONVIF device address, profile tokens and stream
host: together those are a map of the camera network."""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.api.cameras import ADDRESS_FIELDS
from app.store import connect
from tests.test_access_grants import GrantCase


class CameraAddressTests(GrantCase):
    def setUp(self):
        super().setUp()
        with connect() as db:
            db.execute("UPDATE cameras SET camera_ip='192.0.2.21',device_service_url='http://192.0.2.21/onvif/device_service',onvif_profile_token='main',"
                       "live_substream_profile_token='sub' WHERE id=?", (self.d1,))

    def camera(self, client: TestClient, **kwargs) -> dict:
        response = client.get("/api/cameras", **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return next(camera for camera in response.json()["cameras"] if camera["id"] == self.d1)

    def test_an_administrator_sees_where_each_camera_is(self):
        camera = self.camera(self.admin)
        self.assertEqual((camera["camera_ip"], camera["device_service_url"], camera["onvif_profile_token"], camera["live_substream_profile_token"]),
                         ("192.0.2.21", "http://192.0.2.21/onvif/device_service", "main", "sub"))
        self.assertTrue(camera["stream_url"].startswith("rtsp://camera.invalid"))

    def test_an_operator_sees_the_camera_but_not_where_it_is(self):
        camera = self.camera(self.limited)
        self.assertEqual({field: camera[field] for field in ADDRESS_FIELDS}, dict.fromkeys(ADDRESS_FIELDS))
        self.assertEqual(camera["name"], "Dock 1")  # Everything else is still there
        self.assertIn("recording_health", camera)
        self.assertNotIn("192.0.2.21", self.limited.get("/api/cameras").text)

    def test_a_read_scope_token_does_not_see_where_cameras_are(self):
        made = self.admin.post("/api/admin/api-tokens", json={"name": "Integration", "scopes": ["read"]})
        self.assertEqual(made.status_code, 201, made.text)
        with TestClient(self.app) as bearer:  # No session cookie: only the token signs this in
            listed = bearer.get("/api/cameras", headers={"Authorization": f"Bearer {made.json()['secret']}"})
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertNotIn("192.0.2.21", listed.text)
        camera = next(camera for camera in listed.json()["cameras"] if camera["id"] == self.d1)
        self.assertEqual({field: camera[field] for field in ADDRESS_FIELDS}, dict.fromkeys(ADDRESS_FIELDS))

    def test_a_token_with_the_administration_scope_does_see_them(self):
        made = self.admin.post("/api/admin/api-tokens", json={"name": "Inventory", "scopes": ["read", "admin"]})
        self.assertEqual(made.status_code, 201, made.text)
        with TestClient(self.app) as bearer:
            listed = bearer.get("/api/cameras", headers={"Authorization": f"Bearer {made.json()['secret']}"})
        self.assertIn("192.0.2.21", listed.text)

    def test_the_rule_for_each_kind_of_caller(self):
        from app.api.cameras import sees_camera_addresses
        from app.principal import Principal

        cases = {
            "administrator's session": (Principal(1, "keeper", "administrator"), True),
            "operator's session": (Principal(2, "op", "operator"), False),
            "read-scope token": (Principal(1, "keeper", "administrator", scopes=frozenset({"read"})), False),
            "read and media token": (Principal(1, "keeper", "administrator", scopes=frozenset({"read", "media"})), False),
            "administration-scope token": (Principal(1, "keeper", "administrator", scopes=frozenset({"read", "admin"})), True),
            "video wall": (Principal(1, "keeper", "administrator", scopes=frozenset({"read", "media"}), wall=True), False),
        }
        for name, (principal, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(sees_camera_addresses(principal), expected)


if __name__ == "__main__":
    unittest.main()
