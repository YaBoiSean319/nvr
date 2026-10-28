"""PTZ auto-tracking (#145): the camera follows a chosen object under the control lease."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import ptz, ptz_tracking, rule_actions
from app.alarms import create_alarm
from app.credentials import encrypt
from app.security import hash_password
from app.store import connect
from tests.fixtures.ptz_device import PtzDevice
from tests.scene_case import SceneTestCase
from tests.test_support import TEST_PASSWORD

BOX = {"x": 0.4, "y": 0.35, "width": 0.1, "height": 0.3}  # Centred and about as big as the default target size wants


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def iso(self) -> str:
        return self.now.isoformat()


class ControlTests(SceneTestCase):
    """The control law on its own."""

    def test_a_centred_target_of_the_right_size_gets_no_command(self):
        self.assertEqual(ptz_tracking.control({"x": 0.4, "y": 0.3, "width": 0.2, "height": 0.4}, target_size=0.4, can_zoom=True), (0.0, 0.0, 0.0))

    def test_the_camera_turns_towards_the_target(self):
        right = ptz_tracking.control({"x": 0.7, "y": 0.4, "width": 0.1, "height": 0.2}, target_size=0.2, can_zoom=False)
        left = ptz_tracking.control({"x": 0.1, "y": 0.4, "width": 0.1, "height": 0.2}, target_size=0.2, can_zoom=False)
        above = ptz_tracking.control({"x": 0.45, "y": 0.05, "width": 0.1, "height": 0.2}, target_size=0.2, can_zoom=False)
        below = ptz_tracking.control({"x": 0.45, "y": 0.75, "width": 0.1, "height": 0.2}, target_size=0.2, can_zoom=False)
        self.assertGreater(right[0], 0)
        self.assertLess(left[0], 0)
        self.assertGreater(above[1], 0)  # Up is positive tilt
        self.assertLess(below[1], 0)
        self.assertTrue(all(abs(value) <= ptz_tracking.MAX_SPEED for command in (right, left, above, below) for value in command))

    def test_zoom_follows_the_size_only_when_the_target_is_near_the_centre(self):
        small = {"x": 0.47, "y": 0.45, "width": 0.05, "height": 0.1}
        large = {"x": 0.3, "y": 0.1, "width": 0.4, "height": 0.8}
        self.assertGreater(ptz_tracking.control(small, target_size=0.4, can_zoom=True)[2], 0)
        self.assertLess(ptz_tracking.control(large, target_size=0.4, can_zoom=True)[2], 0)
        self.assertEqual(ptz_tracking.control(small, target_size=0.4, can_zoom=False)[2], 0)
        far = {"x": 0.85, "y": 0.45, "width": 0.05, "height": 0.1}
        self.assertEqual(ptz_tracking.control(far, target_size=0.4, can_zoom=True)[2], 0)  # Off to the side: first turn, then zoom


class TrackingTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        self.device = PtzDevice()
        self.addCleanup(self.device.close)
        ptz.forget_capabilities()
        with connect() as db:
            db.execute("UPDATE cameras SET device_service_url=?,motion_credentials_ciphertext=? WHERE id=?",
                       (self.device.url, encrypt(json.dumps({"username": "onvif", "password": "camera-pass"})), self.camera_id))
        self.clock = Clock()
        for target in ("app.ptz._now", "app.ptz_tracking._now"):
            patcher = patch(target, self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        ptz_tracking._failures.clear()
        self.base = f"/api/cameras/{self.camera_id}/ptz"

    # --- helpers ---------------------------------------------------------------------------------------------------------------------------

    def operator(self, name: str = "watcher") -> TestClient:
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", (name, hash_password(TEST_PASSWORD), "operator"))
        context = TestClient(self.client.app)
        client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.assertEqual(client.post("/api/auth/login", json={"username": name, "password": TEST_PASSWORD}).status_code, 200)
        return client

    def track(self, label: str = "person") -> str:
        track_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,detection_count,max_confidence,model_id,model_version,provider) VALUES(?,?,?,?,?,1,0.9,'m','1','cpu')",
                       (track_id, self.camera_id, label, self.clock.iso(), self.clock.iso()))
        return track_id

    def see(self, track_id: str, box: dict | None = None, label: str = "person") -> None:
        box = box or BOX
        with connect() as db:
            db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                       "VALUES(?,?,?,?,0.9,?,?,?,?,'m','1','cpu')", (self.camera_id, track_id, self.clock.iso(), label, box["x"], box["y"], box["width"], box["height"]))
            db.execute("UPDATE ai_tracks SET last_seen_at=? WHERE id=?", (self.clock.iso(), track_id))

    def start(self, track_id: str, client: TestClient | None = None):
        return (client or self.client).post(f"{self.base}/track", json={"track_id": track_id})

    def tick(self) -> dict[str, int]:
        return asyncio.run(ptz_tracking.tick(self.clock.now))

    def tracking(self):
        with connect() as db:
            return db.execute("SELECT * FROM ptz_tracking WHERE camera_id=?", (self.camera_id,)).fetchone()

    def lease(self):
        with connect() as db:
            return db.execute("SELECT * FROM ptz_leases WHERE camera_id=?", (self.camera_id,)).fetchone()

    def audit(self) -> list[tuple[str, dict]]:
        with connect() as db:
            return [(row["action"], json.loads(row["details_json"])) for row in db.execute("SELECT action,details_json FROM audit_log WHERE action LIKE 'ptz_tracking%' ORDER BY id")]

    def preset(self, name: str = "Home") -> dict:
        created = self.client.post(f"{self.base}/presets", json={"name": name})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    # --- keeping the target near the centre ----------------------------------------------------------------------------------------------------

    def test_the_target_stays_near_the_centre_of_the_picture(self):
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        applied = 0
        camera_pan = 0.0
        world_x = 0.0  # The target starts at the centre of the picture and walks away from where the camera points
        offsets: list[float] = []
        for step in range(60):
            self.clock.advance(0.5)
            world_x += 0.5 * 0.035
            centre_x = 0.5 + (world_x - camera_pan)  # Where the target is in the picture now
            self.see(track_id, {"x": centre_x - 0.05, "y": 0.35, "width": 0.1, "height": 0.3})
            self.tick()
            for command in self.device.commands[applied:]:
                if command["op"] == "ContinuousMove" and command["pan_tilt"]:
                    camera_pan += float(command["pan_tilt"]["x"]) * 0.45 * 0.5
            applied = len(self.device.commands)
            offsets.append(abs(centre_x - 0.5))
        self.assertGreater(world_x, 0.9)
        self.assertLess(max(offsets[10:]), 0.12)  # Within a few percent of the picture's width of the centre once it has caught up
        self.assertLess(max(offsets), 0.2)
        # Every move command went to the camera's PTZ profile, never longer than the move limit, and none after the last sighting was stale.
        self.assertTrue(all(command["profile"] == "profile-main" and float(command["timeout"].strip("PTS")) <= 1.0 for command in self.device.commands))

    # --- the lease -----------------------------------------------------------------------------------------------------------------------------

    def test_tracking_holds_the_lease_and_never_starts_without_it(self):
        other = self.operator("driver")
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(other.post(f"{self.base}/move", json={"pan": 1, "tilt": 0, "zoom": 0}).status_code, 200)
        before = len(self.device.commands)
        refused = self.start(track_id)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertIn("driver", refused.json()["detail"])
        self.assertIsNone(self.tracking())
        self.assertEqual(self.lease()["purpose"], "manual")  # The refused start left the driver's lease as it was, and took none for itself
        self.assertNotEqual(self.lease()["user_id"], 1)
        self.clock.advance(0.5)
        self.tick()
        self.assertEqual(len(self.device.commands), before)  # Nothing was sent: there is no tracking and the lease is the driver's

        # Once the driver's lease has lapsed, tracking can start, and it then holds a tracking lease for the person who chose the target.
        self.clock.advance(ptz.LEASE_SECONDS + 1)
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        lease = self.lease()
        self.assertEqual((lease["purpose"], lease["user_id"]), ("tracking", self.tracking()["lease_user_id"]))
        state = self.client.get(self.base).json()
        self.assertTrue(state["lease"]["tracking"])
        self.assertEqual(state["tracking"]["track_id"], track_id)

    def test_the_lease_is_renewed_while_tracking_and_tracking_ends_if_it_is_lost(self):
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        for _ in range(130):  # Well past the lease's own length: only the renewals keep it
            self.clock.advance(0.5)
            self.see(track_id, {"x": 0.6, "y": 0.35, "width": 0.1, "height": 0.3})
            self.tick()
        self.assertIsNotNone(self.tracking())
        self.assertGreater(len(self.device.commands), 5)

        # The lease vanishes (an administrator's take-over, a restore, a deleted row): the next pass sends nothing and ends the tracking.
        with connect() as db:
            db.execute("DELETE FROM ptz_leases")
        sent = len(self.device.commands)
        self.clock.advance(0.5)
        self.see(track_id, {"x": 0.7, "y": 0.35, "width": 0.1, "height": 0.3})
        self.assertEqual(self.tick(), {"abandon": 1})
        self.assertEqual(len(self.device.commands), sent)
        self.assertIsNone(self.tracking())
        self.assertIn(("ptz_tracking_ended", {"reason": "lease_lost", "source": "operator", "track_id": track_id}), self.audit())

    def test_an_expired_lease_is_never_steered_with(self):
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        self.clock.advance(ptz.LEASE_SECONDS + 5)  # The tracker was not running for a while
        self.see(track_id, {"x": 0.7, "y": 0.35, "width": 0.1, "height": 0.3})
        self.assertEqual(self.tick(), {"abandon": 1})
        self.assertEqual(self.device.commands, [])

    # --- manual control -------------------------------------------------------------------------------------------------------------------------

    def test_manual_control_interrupts_tracking_at_once(self):
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        self.clock.advance(0.5)
        self.see(track_id, {"x": 0.7, "y": 0.35, "width": 0.1, "height": 0.3})
        self.assertEqual(self.tick(), {"move": 1})

        # Another operator takes the controls: no administrator needed, and the tracking is gone before their command even returns.
        driver = self.operator("driver")
        self.assertEqual(driver.post(f"{self.base}/move", json={"pan": -1, "tilt": 0, "zoom": 0}).status_code, 200)
        self.assertIsNone(self.tracking())
        self.assertEqual((self.lease()["purpose"], self.client.get(self.base).json()["lease"]["holder"]), ("manual", "driver"))
        sent = len(self.device.commands)
        for _ in range(4):
            self.clock.advance(0.5)
            self.see(track_id, {"x": 0.8, "y": 0.35, "width": 0.1, "height": 0.3})
            self.tick()
        self.assertEqual(len(self.device.commands), sent)  # The camera is the driver's, not the tracker's
        self.assertIn("ptz_tracking_interrupted", [action for action, _ in self.audit()])

    def test_the_person_who_started_tracking_can_take_the_controls_back(self):
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        self.assertEqual(self.client.post(f"{self.base}/stop").status_code, 200)
        self.assertIsNone(self.tracking())
        self.assertEqual(self.lease()["purpose"], "manual")

    def test_stopping_tracking_stops_the_camera_and_lets_go_of_the_lease(self):
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        self.assertEqual(self.client.delete(f"{self.base}/track").status_code, 204)
        self.assertIsNone(self.tracking())
        self.assertIsNone(self.lease())
        self.assertEqual(self.device.ops()[-1], "Stop")

    def test_choosing_another_object_switches_the_target(self):
        first, second = self.track(), self.track("vehicle")
        self.see(first)
        self.see(second, label="vehicle")
        self.assertEqual(self.start(first).status_code, 200)
        self.assertEqual(self.start(second).status_code, 200)
        self.assertEqual(self.tracking()["track_id"], second)

    # --- losing the target ----------------------------------------------------------------------------------------------------------------------

    def test_a_lost_target_stops_the_camera_then_sends_it_home_after_the_timeout(self):
        home = self.preset("Home")
        self.assertEqual(self.client.put(f"{self.base}/tracking", json={"home_preset_id": home["id"], "lost_seconds": 3, "home_timeout_seconds": 20, "target_size": 0.4, "zoom_enabled": True}).status_code, 200)
        self.device.commands.clear()
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        self.clock.advance(0.5)
        self.see(track_id, {"x": 0.7, "y": 0.35, "width": 0.1, "height": 0.3})
        self.tick()
        self.clock.advance(2)  # Not seen for 2 seconds: still just waiting
        self.assertEqual(self.tick(), {"wait": 1})
        self.clock.advance(2)  # 4 seconds: lost
        self.assertEqual(self.tick(), {"stop": 1})
        self.assertEqual(self.tracking()["state"], "lost")
        self.assertEqual(self.device.ops()[-1], "Stop")
        sent = len(self.device.commands)
        self.clock.advance(10)
        self.assertEqual(self.tick(), {"wait": 1})
        self.assertEqual(len(self.device.commands), sent)  # Nothing more while it waits
        self.assertIsNotNone(self.lease())  # And it keeps the lease while it waits: nobody else is moved aside by a pause

        self.clock.advance(10)  # Lost for more than the timeout
        self.assertEqual(self.tick(), {"home": 1})
        self.assertEqual(self.device.commands[-1], {"op": "GotoPreset", "profile": "profile-main", "token": home["token"]})
        self.assertIsNone(self.tracking())
        self.assertIsNone(self.lease())
        self.assertIn(("ptz_tracking_ended", {"reason": "lost_returned_home", "source": "operator", "track_id": track_id}), self.audit())

    def test_a_target_that_comes_back_in_time_is_followed_again(self):
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        self.clock.advance(7)
        self.assertEqual(self.tick(), {"stop": 1})
        self.clock.advance(5)  # Back within the home timeout
        self.see(track_id, {"x": 0.75, "y": 0.35, "width": 0.1, "height": 0.3})
        self.assertEqual(self.tick(), {"move": 1})
        row = self.tracking()
        self.assertEqual((row["state"], row["lost_since"]), ("tracking", None))

    def test_without_a_home_preset_a_lost_target_just_ends_the_tracking(self):
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        self.clock.advance(6)
        self.tick()
        self.clock.advance(31)
        self.assertEqual(self.tick(), {"home": 1})
        self.assertNotIn("GotoPreset", self.device.ops())
        self.assertIsNone(self.tracking())
        self.assertIsNone(self.lease())

    def test_a_target_in_a_privacy_mask_counts_as_not_seen(self):
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        mask = [{"x": 0.6, "y": 0.0, "width": 0.4, "height": 1.0}]
        with patch("app.ptz_tracking.privacy.server_zones", return_value=mask):
            for _ in range(3):
                self.clock.advance(2)
                self.see(track_id, {"x": 0.7, "y": 0.35, "width": 0.1, "height": 0.3})
                self.tick()
            self.assertEqual(self.tracking()["state"], "lost")
            self.assertNotIn("ContinuousMove", self.device.ops())  # The camera never followed what the mask hides
            self.assertEqual(self.client.get(f"{self.base}/targets").json()["objects"], [])
            self.assertEqual(self.start(self.track()).status_code, 409)  # Nor can one in the mask be chosen

    # --- unreachable camera ---------------------------------------------------------------------------------------------------------------------

    def test_a_camera_that_stops_answering_ends_the_tracking(self):
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        self.device.close()
        for _ in range(ptz_tracking.FAILURES_BEFORE_GIVING_UP):
            self.clock.advance(0.5)
            self.see(track_id, {"x": 0.8, "y": 0.35, "width": 0.1, "height": 0.3})
            self.tick()
        self.assertIsNone(self.tracking())
        self.assertIsNone(self.lease())
        self.assertIn("camera_unreachable", [details.get("reason") for _, details in self.audit()])

    def test_a_home_move_the_camera_did_not_get_is_not_reported_as_done(self):
        home = self.preset("Home")
        self.assertEqual(self.client.put(f"{self.base}/tracking", json={"home_preset_id": home["id"], "lost_seconds": 1, "home_timeout_seconds": 5, "target_size": 0.4, "zoom_enabled": True}).status_code, 200)
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        self.clock.advance(2)
        self.assertEqual(self.tick(), {"stop": 1})
        self.clock.advance(6)
        self.device.close()
        self.assertEqual(self.tick(), {"failed": 1})
        self.assertIsNotNone(self.tracking())  # Not ended: the camera never moved home, so it is tried again
        for _ in range(ptz_tracking.FAILURES_BEFORE_GIVING_UP - 1):
            self.clock.advance(0.5)
            self.tick()
        self.assertIsNone(self.tracking())
        reasons = [details.get("reason") for _, details in self.audit() if details.get("reason")]
        self.assertEqual(reasons, ["camera_unreachable"])  # Never "returned home"
        self.assertEqual(ptz_tracking._failures, {})

    def test_replacing_a_target_is_recorded(self):
        first, second = self.track(), self.track()
        self.see(first)
        self.see(second)
        self.assertEqual(self.start(first).status_code, 200)
        self.assertEqual(self.start(second).status_code, 200)
        self.assertEqual([(action, details.get("reason")) for action, details in self.audit()],
                         [("ptz_tracking_started", None), ("ptz_tracking_ended", "replaced"), ("ptz_tracking_started", None)])

    # --- what can be chosen ---------------------------------------------------------------------------------------------------------------------

    def test_only_an_object_in_view_on_this_camera_can_be_chosen(self):
        gone = self.track()
        with connect() as db:
            db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                       "VALUES(?,?,?,'person',0.9,0.4,0.4,0.1,0.2,'m','1','cpu')", (self.camera_id, gone, (self.clock.now - timedelta(minutes=5)).isoformat()))
        self.assertEqual(self.start(gone).status_code, 409)
        self.assertEqual(self.start(str(uuid.uuid4())).status_code, 409)
        other_camera = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (other_camera, "Other", encrypt("rtsp://camera.invalid/other")))
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,detection_count,max_confidence,model_id,model_version,provider) VALUES('elsewhere',?,'person',?,?,1,0.9,'m','1','cpu')",
                       (other_camera, self.clock.iso(), self.clock.iso()))
        self.assertEqual(self.start("elsewhere").status_code, 409)
        self.assertEqual(self.lease(), None)

    def test_the_objects_in_view_can_be_listed_for_choosing(self):
        track_id = self.track()
        self.see(track_id)
        with patch("app.api.detections.datetime") as fake:
            fake.now.return_value = self.clock.now
            fake.fromisoformat = datetime.fromisoformat
            listed = self.client.get(f"{self.base}/targets")
        self.assertEqual(listed.status_code, 200, listed.text)
        body = listed.json()
        self.assertEqual([(item["track_id"], item["label"]) for item in body["objects"]], [(track_id, "person")])
        self.assertEqual(body["objects"][0]["box"], BOX)
        self.assertIsNone(body["tracking"])

    def test_a_camera_that_cannot_pan_or_tilt_is_refused(self):
        with connect() as db:
            db.execute("UPDATE cameras SET device_service_url=NULL WHERE id=?", (self.camera_id,))
        ptz.forget_capabilities()
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 422)

    # --- settings -------------------------------------------------------------------------------------------------------------------------------

    def test_administrators_set_the_home_preset_and_timeouts(self):
        home = self.preset()
        saved = self.client.put(f"{self.base}/tracking", json={"home_preset_id": home["id"], "lost_seconds": 8, "home_timeout_seconds": 90, "target_size": 0.5, "zoom_enabled": False})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.client.get(self.base).json()["tracking_settings"], {"home_preset_id": home["id"], "lost_seconds": 8.0, "home_timeout_seconds": 90, "target_size": 0.5, "zoom_enabled": False})
        self.assertIn("ptz_tracking_settings_changed", [row["action"] for row in self._audit_rows()])
        # A preset of another camera, and values out of range, are refused; operators cannot change them.
        self.assertEqual(self.client.put(f"{self.base}/tracking", json={"home_preset_id": "nope"}).status_code, 422)
        self.assertEqual(self.client.put(f"{self.base}/tracking", json={"lost_seconds": 0}).status_code, 422)
        self.assertEqual(self.operator().put(f"{self.base}/tracking", json={}).status_code, 403)
        # Deleting the preset leaves the setting empty rather than dangling.
        self.assertEqual(self.client.delete(f"{self.base}/presets/{home['id']}").status_code, 204)
        self.assertIsNone(self.client.get(self.base).json()["tracking_settings"]["home_preset_id"])

    def _audit_rows(self):
        with connect() as db:
            return db.execute("SELECT action FROM audit_log").fetchall()

    def test_tours_wait_while_a_camera_is_tracking(self):
        home = self.preset("A")
        other = self.preset("B")
        tour = self.client.post(f"{self.base}/tours", json={"name": "Patrol", "enabled": True, "steps": [{"preset_id": home["id"], "dwell_seconds": 3}, {"preset_id": other["id"], "dwell_seconds": 3}]})
        self.assertEqual(tour.status_code, 201, tour.text)
        track_id = self.track()
        self.see(track_id)
        self.assertEqual(self.start(track_id).status_code, 200)
        self.device.commands.clear()
        asyncio.run(ptz.tick_tours())
        self.assertNotIn("GotoPreset", self.device.ops())

    # --- rules ----------------------------------------------------------------------------------------------------------------------------------

    def rule_event(self, track_id: str, rule_id: str) -> str:
        event_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json) VALUES(?,?,?,?,?,?,?,?,?)",
                       (event_id, rule_id, "Gate entry", self.camera_id, track_id, "person", "zone_entry", self.clock.iso(), json.dumps(BOX)))
            create_alarm(db, event_id)
        return event_id

    def follow_rule(self) -> dict:
        response = self.client.post("/api/rules", json={
            "name": "Follow intruders", "enabled": True, "camera_ids": [self.camera_id], "group_ids": [], "classes": ["person"],
            "condition": {"type": "zone_entry", "zone": {"x": 0.0, "y": 0.6, "width": 0.3, "height": 0.4}}, "schedule": None, "cooldown_seconds": 0,
            "actions": [{"type": "ptz_track", "name": "Follow it"}]})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def deliver(self) -> dict:
        asyncio.run(rule_actions.deliver_due())
        with connect() as db:
            return {row["rule_event_id"]: (row["status"], row["last_error"]) for row in db.execute("SELECT rule_event_id,status,last_error FROM rule_action_deliveries")}

    def test_a_rule_can_start_tracking_the_object_that_triggered_it(self):
        rule = self.follow_rule()
        self.assertEqual(rule["actions"][0]["type"], "ptz_track")
        track_id = self.track()
        self.see(track_id)
        event = self.rule_event(track_id, rule["id"])
        self.assertEqual(self.deliver(), {event: ("delivered", None)})
        row = self.tracking()
        self.assertEqual((row["track_id"], row["source"], row["rule_id"]), (track_id, "rule", rule["id"]))
        self.assertEqual(self.lease()["purpose"], "tracking")
        self.clock.advance(0.5)
        self.see(track_id, {"x": 0.75, "y": 0.35, "width": 0.1, "height": 0.3})
        self.assertEqual(self.tick(), {"move": 1})
        self.assertEqual(self.device.ops()[-1], "ContinuousMove")

    def test_a_rule_never_takes_a_camera_from_a_person_or_from_other_tracking(self):
        rule = self.follow_rule()
        driver = self.operator("driver")
        self.assertEqual(driver.post(f"{self.base}/move", json={"pan": 1, "tilt": 0, "zoom": 0}).status_code, 200)
        first = self.track()
        self.see(first)
        event = self.rule_event(first, rule["id"])
        self.assertEqual(self.deliver()[event], ("failed", "ptz_lease_held"))
        self.assertIsNone(self.tracking())

        self.clock.advance(ptz.LEASE_SECONDS + 1)
        second, third = self.track("vehicle"), self.track()
        self.see(second, label="vehicle")
        self.see(third)
        second_event = self.rule_event(second, rule["id"])
        results = self.deliver()
        self.assertEqual(results[second_event], ("delivered", None))
        third_event = self.rule_event(third, rule["id"])
        results = self.deliver()
        self.assertEqual(results[third_event], ("failed", "ptz_already_tracking"))  # The first object stays the target
        self.assertEqual(self.tracking()["track_id"], second)

        # A person choosing something else does take it.
        self.assertEqual(self.start(third).status_code, 200)
        self.assertEqual((self.tracking()["track_id"], self.tracking()["source"]), (third, "operator"))

    def test_a_rule_on_a_camera_that_cannot_move_fails_plainly(self):
        rule = self.follow_rule()
        with connect() as db:
            db.execute("UPDATE cameras SET device_service_url=NULL WHERE id=?", (self.camera_id,))
        ptz.forget_capabilities()
        track_id = self.track()
        self.see(track_id)
        event = self.rule_event(track_id, rule["id"])
        self.assertEqual(self.deliver()[event], ("failed", "ptz_not_controllable"))
