"""PTZ control with presets, tours and a control lease (#114)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import ptz
from app.credentials import encrypt
from app.security import hash_password
from app.store import connect
from tests.fixtures.ptz_device import PtzDevice
from tests.scene_case import SceneTestCase
from tests.test_support import TEST_PASSWORD


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class PtzTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        self.device = PtzDevice()
        self.addCleanup(self.device.close)
        ptz.forget_capabilities()
        with connect() as db:
            db.execute("UPDATE cameras SET device_service_url=?,motion_credentials_ciphertext=? WHERE id=?",
                       (self.device.url, encrypt(json.dumps({"username": "onvif", "password": "camera-pass"})), self.camera_id))
        self.clock = Clock()
        clock = patch("app.ptz._now", self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.base = f"/api/cameras/{self.camera_id}/ptz"

    def operator(self, name: str = "watcher") -> TestClient:
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", (name, hash_password(TEST_PASSWORD), "operator"))
        context = TestClient(self.client.app)
        client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.assertEqual(client.post("/api/auth/login", json={"username": name, "password": TEST_PASSWORD}).status_code, 200)
        return client

    def preset(self, name: str) -> dict:
        created = self.client.post(f"{self.base}/presets", json={"name": name})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def test_moves_zoom_centring_and_presets_reach_the_camera(self):
        state = self.client.get(self.base)
        self.assertEqual(state.status_code, 200, state.text)
        body = state.json()
        self.assertEqual((body["supported"], body["capabilities"]["pan_tilt"], body["capabilities"]["zoom"], body["capabilities"]["centre"]), (True, True, True, True))
        self.assertIsNone(body["lease"])

        self.assertEqual(self.client.post(f"{self.base}/move", json={"pan": 0.5, "tilt": -0.25, "zoom": 0}).status_code, 200)
        self.assertEqual(self.client.post(f"{self.base}/move", json={"pan": 0, "tilt": 0, "zoom": 1}).status_code, 200)
        self.assertEqual(self.client.post(f"{self.base}/stop").status_code, 200)
        self.assertEqual(self.client.post(f"{self.base}/centre", json={"x": 0.75, "y": 0.25}).status_code, 200)
        gate = self.preset("Gate")
        self.assertEqual(self.client.patch(f"{self.base}/presets/{gate['id']}", json={"name": "Front gate"}).json()["name"], "Front gate")
        self.assertEqual(self.client.post(f"{self.base}/presets/{gate['id']}/go").status_code, 200)
        self.assertEqual(self.client.delete(f"{self.base}/presets/{gate['id']}").status_code, 204)

        commands = self.device.commands
        self.assertEqual(self.device.ops(), ["ContinuousMove", "ContinuousMove", "Stop", "RelativeMove", "SetPreset", "GotoPreset", "RemovePreset"])
        self.assertTrue(all(command["profile"] == "profile-main" for command in commands))
        self.assertEqual((commands[0]["pan_tilt"], commands[0]["zoom"]), ({"x": "0.5000", "y": "-0.2500"}, None))
        self.assertEqual((commands[1]["pan_tilt"], commands[1]["zoom"]), (None, {"x": "1.0000"}))
        self.assertEqual(commands[3]["pan_tilt"], {"x": "0.5000", "y": "0.5000", "space": "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov"})
        self.assertEqual((commands[4]["name"], commands[5]["token"], commands[6]["token"]), ("Gate", commands[4]["token"], commands[4]["token"]))
        self.assertEqual(self.client.get(self.base).json()["presets"], [])

    def test_one_person_controls_a_camera_at_a_time(self):
        operator = self.operator()
        other = self.operator("second")
        self.assertEqual(operator.post(f"{self.base}/move", json={"pan": 1, "tilt": 0, "zoom": 0}).status_code, 200)
        lease = operator.get(self.base).json()["lease"]
        self.assertEqual((lease["holder"], lease["mine"]), ("watcher", True))

        refused = other.post(f"{self.base}/move", json={"pan": -1, "tilt": 0, "zoom": 0})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("watcher", refused.json()["detail"])
        self.assertEqual(other.get(self.base).json()["lease"]["holder"], "watcher")
        # Only administrators take over.
        self.assertEqual(other.post(f"{self.base}/lease", json={"take_over": True}).status_code, 403)

        # Each command extends the lease; it expires after the timeout without one.
        self.clock.advance(ptz.LEASE_SECONDS - 1)
        self.assertEqual(operator.post(f"{self.base}/stop").status_code, 200)
        self.clock.advance(ptz.LEASE_SECONDS - 1)
        self.assertEqual(other.post(f"{self.base}/move", json={"pan": -1, "tilt": 0, "zoom": 0}).status_code, 409)
        self.clock.advance(2)
        self.assertEqual(other.post(f"{self.base}/move", json={"pan": -1, "tilt": 0, "zoom": 0}).status_code, 200)

        # An administrator takes over; the previous holder then cannot move.
        self.assertEqual(self.client.post(f"{self.base}/move", json={"pan": 0, "tilt": 1, "zoom": 0}).status_code, 409)
        taken = self.client.post(f"{self.base}/lease", json={"take_over": True})
        self.assertEqual(taken.status_code, 200, taken.text)
        self.assertEqual(taken.json()["holder"], "keeper")
        self.assertEqual(other.post(f"{self.base}/move", json={"pan": -1, "tilt": 0, "zoom": 0}).status_code, 409)
        self.assertEqual(self.client.post(f"{self.base}/move", json={"pan": 0, "tilt": 1, "zoom": 0}).status_code, 200)
        self.assertEqual(self.client.delete(f"{self.base}/lease").status_code, 204)
        self.assertIsNone(other.get(self.base).json()["lease"])

    def test_operators_move_cameras_but_only_administrators_edit_presets_and_tours(self):
        gate = self.preset("Gate")
        operator = self.operator()
        self.assertEqual(operator.post(f"{self.base}/presets/{gate['id']}/go").status_code, 200)
        self.assertEqual(operator.post(f"{self.base}/presets", json={"name": "Mine"}).status_code, 403)
        self.assertEqual(operator.patch(f"{self.base}/presets/{gate['id']}", json={"name": "Mine"}).status_code, 403)
        self.assertEqual(operator.delete(f"{self.base}/presets/{gate['id']}").status_code, 403)
        self.assertEqual(operator.post(f"{self.base}/tours", json={"name": "Patrol", "enabled": True, "steps": [{"preset_id": gate["id"], "dwell_seconds": 10}], "schedule": None}).status_code, 403)

    def test_tours_patrol_presets_and_pause_while_someone_holds_the_lease(self):
        gate, yard = self.preset("Gate"), self.preset("Yard")
        tour = self.client.post(f"{self.base}/tours", json={
            "name": "Patrol", "enabled": True, "schedule": None,
            "steps": [{"preset_id": gate["id"], "dwell_seconds": 20}, {"preset_id": yard["id"], "dwell_seconds": 30}],
        })
        self.assertEqual(tour.status_code, 201, tour.text)
        self.device.commands.clear()

        def tick():
            asyncio.run(ptz.tick_tours())

        def visited():
            return [command["token"] for command in self.device.commands if command["op"] == "GotoPreset"]

        tick()
        self.assertEqual(visited(), [gate["token"]])
        self.clock.advance(19)
        tick()
        self.assertEqual(visited(), [gate["token"]])
        self.clock.advance(1)
        tick()
        self.assertEqual(visited(), [gate["token"], yard["token"]])

        # A person takes control: the tour waits, then carries on with its next step.
        operator = self.operator()
        operator.post(f"{self.base}/move", json={"pan": 1, "tilt": 0, "zoom": 0})
        self.clock.advance(30)
        tick()
        self.assertEqual(visited(), [gate["token"], yard["token"]])
        self.assertEqual(self.client.get(self.base).json()["tours"][0]["state"], "paused")
        operator.delete(f"{self.base}/lease")
        tick()
        self.assertEqual(visited(), [gate["token"], yard["token"], gate["token"]])
        self.assertEqual(self.client.get(self.base).json()["tours"][0]["state"], "running")

        # Outside its schedule a tour does nothing.
        payload = {**tour.json(), "schedule": {"timezone": "UTC", "intervals": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "01:00", "end": "02:00"}]}}
        saved = self.client.put(f"{self.base}/tours/{tour.json()['id']}", json={key: payload[key] for key in ("name", "enabled", "steps", "schedule")})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.clock.advance(60)
        tick()
        self.assertEqual(len(visited()), 3)

    def test_one_enabled_tour_per_camera_and_steps_name_this_cameras_presets(self):
        gate = self.preset("Gate")
        first = {"name": "A", "enabled": True, "schedule": None, "steps": [{"preset_id": gate["id"], "dwell_seconds": 10}]}
        self.assertEqual(self.client.post(f"{self.base}/tours", json=first).status_code, 201)
        self.assertEqual(self.client.post(f"{self.base}/tours", json={**first, "name": "B"}).status_code, 409)
        self.assertEqual(self.client.post(f"{self.base}/tours", json={**first, "name": "C", "enabled": False}).status_code, 201)
        self.assertEqual(self.client.post(f"{self.base}/tours", json={**first, "name": "D", "enabled": False, "steps": [{"preset_id": "elsewhere", "dwell_seconds": 10}]}).status_code, 422)
        # A preset used by a tour cannot be deleted.
        self.assertEqual(self.client.delete(f"{self.base}/presets/{gate['id']}").status_code, 409)

    def test_a_tour_skips_a_step_whose_preset_is_gone_and_names_ignore_case(self):
        gate, yard = self.preset("Gate"), self.preset("Yard")
        self.assertEqual(self.client.post(f"{self.base}/presets", json={"name": "gate"}).status_code, 409)
        tour = self.client.post(f"{self.base}/tours", json={"name": "Patrol", "enabled": True, "schedule": None,
                                                           "steps": [{"preset_id": gate["id"], "dwell_seconds": 10}, {"preset_id": yard["id"], "dwell_seconds": 10}]})
        self.assertEqual(tour.status_code, 201, tour.text)
        with connect() as db:  # As if the preset went while the tour was being saved.
            db.execute("DELETE FROM ptz_presets WHERE id=?", (gate["id"],))
        self.device.commands.clear()
        asyncio.run(ptz.tick_tours())
        self.assertEqual([command["token"] for command in self.device.commands if command["op"] == "GotoPreset"], [yard["token"]])

    def test_a_camera_without_ptz_says_so(self):
        plain = PtzDevice()
        self.addCleanup(plain.close)
        with connect() as db:
            db.execute("UPDATE cameras SET device_service_url=NULL WHERE id=?", (self.camera_id,))
        body = self.client.get(self.base).json()
        self.assertEqual((body["supported"], body["reason"]), (False, "This camera has no ONVIF device service."))
        self.assertEqual(self.client.post(f"{self.base}/move", json={"pan": 1, "tilt": 0, "zoom": 0}).status_code, 409)
