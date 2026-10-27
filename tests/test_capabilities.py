"""Camera capability health (#173): separate states, backed-off rechecks, save-anyway, and a recheck on demand."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

from app import capabilities
from app.store import connect, initialize
from tests.fixtures.onvif_camera import OnvifCamera
from tests.test_access_grants import GrantCase

USER, PASSWORD = "operator", "S3cret!pass"


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.saved = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")}
        os.environ.update({"NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_RECORDINGS_PATH": str(root / "recordings"), "NVR_CREDENTIAL_KEY_PATH": str(root / "credentials.key")})
        initialize()
        self.camera_id = self.add_camera("Lobby")

    def tearDown(self):
        for key, value in self.saved.items():
            os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
        self.temporary.cleanup()

    def add_camera(self, name: str, **columns) -> str:
        from app.credentials import encrypt
        camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,enabled) VALUES(?,?,?,?)", (camera_id, name, encrypt("rtsp://192.0.2.9/stream"), columns.get("enabled", 1)))
        return camera_id


class RecordTests(StoreCase):
    def test_a_camera_with_nothing_recorded_shows_every_capability_unknown(self):
        with connect() as db:
            shown = capabilities.view(db, self.camera_id)
        self.assertEqual(list(shown), list(capabilities.CAPABILITIES))
        self.assertTrue(all(item["state"] == "unknown" for item in shown.values()))

    def test_capabilities_are_judged_separately(self):
        with connect() as db:
            capabilities.record(db, self.camera_id, "video", "ok", "Video is flowing")
            capabilities.record(db, self.camera_id, "motion", "failed", "Camera authentication rejected")
            shown = capabilities.view(db, self.camera_id)
        self.assertEqual((shown["video"]["state"], shown["motion"]["state"]), ("ok", "failed"))
        self.assertEqual(shown["motion"]["message"], "Camera authentication rejected")

    def test_failures_back_off_and_a_success_starts_again(self):
        with connect() as db:
            gaps = []
            for _ in range(4):
                capabilities.record(db, self.camera_id, "video", "failed", "No answer")
                row = capabilities.view(db, self.camera_id)["video"]
                gaps.append((datetime.fromisoformat(row["next_retry_at"]) - datetime.now(timezone.utc)).total_seconds())
            self.assertEqual(row["failures"], 4)
            capabilities.record(db, self.camera_id, "video", "ok")
            ok = capabilities.view(db, self.camera_id)["video"]
        self.assertTrue(gaps[0] < gaps[1] < gaps[2] < gaps[3])
        self.assertEqual((ok["failures"], ok["next_retry_at"]), (0, None))

    def test_the_wait_grows_but_is_bounded_with_jitter(self):
        self.assertTrue(24 <= capabilities.retry_delay(1) <= 36)
        for failures in (10, 50, 500):
            self.assertLessEqual(capabilities.retry_delay(failures), capabilities.MAX_RETRY_SECONDS * 1.2)
            self.assertGreaterEqual(capabilities.retry_delay(failures), capabilities.MAX_RETRY_SECONDS * 0.8)

    def test_unsupported_and_untestable_unknown_capabilities_are_never_rechecked(self):
        with connect() as db:
            capabilities.record(db, self.camera_id, "motion", "unsupported", "No ONVIF event service is set")
            capabilities.record(db, self.camera_id, "authentication", "unknown", "Not checked")
            shown = capabilities.view(db, self.camera_id)
        self.assertEqual((shown["motion"]["next_retry_at"], shown["authentication"]["next_retry_at"]), (None, None))

    def test_unknown_names_and_states_are_refused(self):
        with connect() as db:
            for args in (("ghost", "ok"), ("video", "great")):
                with self.assertRaises(ValueError):
                    capabilities.record(db, self.camera_id, *args)

    def test_a_report_from_an_inspection_is_stored_capability_by_capability(self):
        report = {"discovery": {"state": "ok", "message": "An ONVIF device service answered"}, "video": {"state": "failed", "message": "No video frame arrived"},
                  "extra": {"state": "ok", "message": ""}}
        with connect() as db:
            capabilities.record_report(db, self.camera_id, report)
            shown = capabilities.view(db, self.camera_id)
        self.assertEqual((shown["discovery"]["state"], shown["video"]["state"], shown["video"]["message"]), ("ok", "failed", "No video frame arrived"))


class DueTests(StoreCase):
    def make_due(self, camera_id: str, capability: str) -> None:
        with connect() as db:
            capabilities.record(db, camera_id, capability, "failed", "x")
            db.execute("UPDATE camera_capabilities SET next_retry_at=? WHERE camera_id=? AND capability=?",
                       ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), camera_id, capability))

    def test_only_failures_whose_time_has_come_are_due_oldest_first(self):
        other = self.add_camera("Dock")
        with connect() as db:
            capabilities.record(db, self.camera_id, "video", "failed", "x")  # Not due yet
        self.make_due(other, "video")
        self.make_due(other, "motion")
        with connect() as db:
            self.assertEqual(sorted(capabilities.due(db)), [(other, "motion"), (other, "video")])

    def test_a_few_are_started_at_a_time_and_disabled_cameras_are_left_alone(self):
        ids = [self.add_camera(f"Cam {number}") for number in range(6)]
        for camera_id in ids:
            self.make_due(camera_id, "video")
        off = self.add_camera("Off", enabled=0)
        self.make_due(off, "video")
        with connect() as db:
            batch = capabilities.due(db)
            self.assertEqual(len(batch), capabilities.BATCH)
            self.assertNotIn((off, "video"), batch)

    def test_capabilities_the_background_cannot_test_alone_are_never_due(self):
        self.make_due(self.camera_id, "ptz")
        with connect() as db:
            self.assertEqual(capabilities.due(db), [])


class RecheckTests(StoreCase, unittest.IsolatedAsyncioTestCase):
    async def test_a_stream_that_opens_is_recorded_ok_and_one_that_does_not_is_recorded_failed(self):
        with mock.patch("app.recording.probe_stream", new=AsyncMock(return_value={"reachable": True})):
            ok = await capabilities.recheck(self.camera_id, "video")
        self.assertEqual(ok["state"], "ok")
        with mock.patch("app.recording.probe_stream", new=AsyncMock(return_value={"reachable": False, "reason": "Camera stream could not be opened"})):
            failed = await capabilities.recheck(self.camera_id, "video")
        self.assertEqual((failed["state"], failed["message"], failed["failures"]), ("failed", "Camera stream could not be opened", 1))

    async def test_a_check_that_blows_up_is_a_failed_check_not_a_crash(self):
        with mock.patch("app.recording.probe_stream", new=AsyncMock(side_effect=RuntimeError("secret detail"))):
            result = await capabilities.recheck(self.camera_id, "video")
        self.assertEqual((result["state"], result["message"]), ("failed", "The check could not be completed"))

    async def test_motion_events_are_tested_against_the_camera(self):
        camera = OnvifCamera(username=USER, password=PASSWORD)
        self.addCleanup(camera.close)
        from app.credentials import encrypt
        import json
        with connect() as db:
            db.execute("UPDATE cameras SET motion_service_url=?,motion_credentials_ciphertext=? WHERE id=?",
                       (camera.url, encrypt(json.dumps({"username": USER, "password": PASSWORD})), self.camera_id))
        self.assertEqual((await capabilities.recheck(self.camera_id, "motion"))["state"], "ok")
        with connect() as db:
            db.execute("UPDATE cameras SET motion_credentials_ciphertext=? WHERE id=?", (encrypt(json.dumps({"username": USER, "password": "wrong"})), self.camera_id))
        wrong = await capabilities.recheck(self.camera_id, "motion")
        self.assertEqual(wrong["state"], "failed")
        self.assertNotIn("wrong", wrong["message"])

    async def test_a_camera_with_no_event_service_has_motion_unsupported(self):
        self.assertEqual((await capabilities.recheck(self.camera_id, "motion"))["state"], "unsupported")

    async def test_only_video_and_motion_can_be_rechecked_and_a_missing_camera_is_none(self):
        self.assertIsNone(await capabilities.recheck(self.camera_id, "ptz"))
        self.assertIsNone(await capabilities.recheck(str(uuid.uuid4()), "video"))

    async def test_the_manager_rechecks_what_is_due_and_nothing_else(self):
        with connect() as db:
            capabilities.record(db, self.camera_id, "video", "failed", "No answer")
            db.execute("UPDATE camera_capabilities SET next_retry_at=? WHERE camera_id=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), self.camera_id))
        with mock.patch("app.recording.probe_stream", new=AsyncMock(return_value={"reachable": True})) as probed:
            self.assertEqual(await capabilities.CapabilityManager().process_once(), 1)
            self.assertEqual(await capabilities.CapabilityManager().process_once(), 0)
        self.assertEqual(probed.await_count, 1)
        with connect() as db:
            self.assertEqual(capabilities.view(db, self.camera_id)["video"]["state"], "ok")


class HealthRobustnessTests(StoreCase, unittest.IsolatedAsyncioTestCase):
    async def test_cameras_saved_before_health_existed_are_checked_once_and_then_kept_up_to_date(self):
        with connect() as db:
            self.assertEqual(capabilities.seed(db), 1)
            self.assertEqual(capabilities.seed(db), 0)
            shown = capabilities.view(db, self.camera_id)
        self.assertEqual((shown["video"]["state"], shown["motion"]["state"]), ("unknown", "unsupported"))
        await asyncio.sleep(capabilities.UNKNOWN_RETRY_SECONDS + 0.2)
        with mock.patch("app.recording.probe_stream", new=AsyncMock(return_value={"reachable": True, "video": {"codec": "h264", "width": 1280, "height": 720}, "audio": True})):
            self.assertEqual(await capabilities.CapabilityManager().process_once(), 1)
        with connect() as db:
            shown = capabilities.view(db, self.camera_id)
            row = db.execute("SELECT video_codec,video_width,audio_supported FROM cameras WHERE id=?", (self.camera_id,)).fetchone()
        self.assertEqual((shown["video"]["state"], shown["audio"]["state"], tuple(row)), ("ok", "available", ("h264", 1280, 1)))
        self.assertIn("recording is off", shown["video"]["message"])  # The camera was added with recording off: say so

    async def test_one_check_that_raises_does_not_stop_the_others(self):
        other = self.add_camera("Dock")
        for camera_id in (self.camera_id, other):
            with connect() as db:
                capabilities.record(db, camera_id, "video", "failed", "x")
                db.execute("UPDATE camera_capabilities SET next_retry_at=? WHERE camera_id=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), camera_id))
        real = capabilities.recheck

        async def flaky(camera_id, capability):
            if camera_id == self.camera_id:
                raise RuntimeError("boom")
            return await real(camera_id, capability)

        with mock.patch("app.capabilities.recheck", new=flaky), mock.patch("app.recording.probe_stream", new=AsyncMock(return_value={"reachable": True})):
            self.assertEqual(await capabilities.CapabilityManager().process_once(), 2)
        with connect() as db:
            self.assertEqual(capabilities.view(db, other)["video"]["state"], "ok")

    async def test_a_camera_deleted_while_it_is_checked_is_not_a_crash(self):
        async def deleted_meanwhile(_stream):
            with connect() as db:
                db.execute("DELETE FROM cameras WHERE id=?", (self.camera_id,))
            return {"reachable": True}
        with mock.patch("app.recording.probe_stream", new=deleted_meanwhile):
            self.assertIsNone(await capabilities.recheck(self.camera_id, "video"))

    async def test_a_check_that_hangs_is_cut_off(self):
        async def hangs(_stream):
            await asyncio.sleep(30)
        with mock.patch.object(capabilities, "CHECK_SECONDS", 0.2), mock.patch("app.recording.probe_stream", new=hangs):
            result = await capabilities.recheck(self.camera_id, "video")
        self.assertEqual((result["state"], result["message"]), ("failed", "The check could not be completed"))

    async def test_an_event_address_with_a_login_in_it_is_refused_without_contacting_anything(self):
        with connect() as db:
            db.execute("UPDATE cameras SET motion_service_url='http://user:pw@127.0.0.1/onvif/device_service' WHERE id=?", (self.camera_id,))
        result = await capabilities.recheck(self.camera_id, "motion")
        self.assertEqual((result["state"], result["message"]), ("failed", "The ONVIF event address is not valid"))
        self.assertNotIn("pw", str(result))

    def test_the_failure_count_is_kept_by_the_database(self):
        with connect() as db:
            for _ in range(3):
                capabilities.record(db, self.camera_id, "motion", "failed", "x")
            self.assertEqual(capabilities.view(db, self.camera_id)["motion"]["failures"], 3)

    def test_capabilities_that_cannot_be_tested_alone_are_ignored_by_due_even_if_marked(self):
        with connect() as db:
            capabilities.record(db, self.camera_id, "ptz", "failed", "x")
            db.execute("UPDATE camera_capabilities SET next_retry_at=? WHERE capability='ptz'", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
            self.assertEqual(capabilities.due(db), [])


class SaveAnywayTests(GrantCase):
    def setUp(self):
        super().setUp()
        self.app.state.recorders = SimpleNamespace(sync_camera=AsyncMock())
        self.camera = OnvifCamera(username=USER, password=PASSWORD)
        self.addCleanup(self.camera.close)

    def body(self, **extra) -> dict:
        return {"name": "Hallway", "camera_ip": "127.0.0.1", "camera_username": USER, "camera_password": PASSWORD, "device_service_url": self.camera.url,
                "recording_enabled": True, "recording_mode": "continuous", **extra}

    def test_a_camera_whose_video_will_not_open_is_refused_unless_saving_anyway(self):
        with mock.patch("app.api.cameras.probe_stream", new=AsyncMock(return_value={"reachable": False, "reason": "Camera stream could not be opened"})):
            refused = self.admin.post("/api/cameras", json=self.body())
            saved = self.admin.post("/api/cameras", json=self.body(save_anyway=True))
        self.assertEqual((refused.status_code, refused.json()["detail"]), (422, "Camera stream could not be opened"))
        self.assertEqual(saved.status_code, 201, saved.text)
        camera = saved.json()
        self.assertFalse(camera["recording_enabled"])  # Not recording a stream that does not open
        shown = self.admin.get(f"/api/cameras/{camera['id']}/capabilities").json()["capabilities"]
        self.assertEqual((shown["video"]["state"], shown["video"]["message"]), ("failed", "Camera stream could not be opened"))
        self.assertIsNotNone(shown["video"]["next_retry_at"])
        self.assertEqual((shown["discovery"]["state"], shown["authentication"]["state"], shown["motion"]["state"]), ("ok", "ok", "ok"))  # From a real inspection: the mock camera accepts a motion subscription
        for secret in (PASSWORD, "rtsp://"):
            self.assertNotIn(secret, self.admin.get(f"/api/cameras/{camera['id']}/capabilities").text)

    def test_a_camera_that_opens_starts_with_video_ok_and_recording_as_asked(self):
        with mock.patch("app.api.cameras.probe_stream", new=AsyncMock(return_value={"reachable": True, "video": {"codec": "h264", "width": 1920, "height": 1080}, "audio": True})):
            saved = self.admin.post("/api/cameras", json=self.body())
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertTrue(saved.json()["recording_enabled"])
        shown = self.admin.get(f"/api/cameras/{saved.json()['id']}/capabilities").json()["capabilities"]
        self.assertEqual((shown["video"]["state"], shown["audio"]["state"]), ("ok", "available"))

    def test_a_recheck_can_be_asked_for_by_an_administrator_only_and_updates_the_state(self):
        with mock.patch("app.api.cameras.probe_stream", new=AsyncMock(return_value={"reachable": False, "reason": "No answer"})):
            camera_id = self.admin.post("/api/cameras", json=self.body(save_anyway=True)).json()["id"]
        self.assertEqual(self.full.post(f"/api/cameras/{camera_id}/capabilities/recheck").status_code, 403)
        self.assertEqual(self.limited.get(f"/api/cameras/{camera_id}/capabilities").status_code, 404)  # A camera the operator may not see is not shown
        with mock.patch("app.recording.probe_stream", new=AsyncMock(return_value={"reachable": True})):
            response = self.admin.post(f"/api/cameras/{camera_id}/capabilities/recheck")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["capabilities"]["video"]["state"], "ok")
        self.assertEqual(len(self.audit("camera_capabilities_rechecked")), 1)

    def stored_stream(self, camera_id: str) -> str:
        from app.credentials import decrypt
        with connect() as db:
            return decrypt(db.execute("SELECT stream_url_ciphertext FROM cameras WHERE id=?", (camera_id,)).fetchone()[0])

    def test_a_camera_whose_stream_address_onvif_will_not_give_is_saved_and_the_address_is_found_later(self):
        broken = OnvifCamera(username=USER, password=PASSWORD, behaviours={"GetProfiles": "fault"})
        self.addCleanup(broken.close)
        body = self.body(device_service_url=broken.url, save_anyway=True)
        refused = self.admin.post("/api/cameras", json={**body, "save_anyway": False})
        self.assertEqual(refused.status_code, 422)
        saved = self.admin.post("/api/cameras", json=body)
        self.assertEqual(saved.status_code, 201, saved.text)
        camera_id = saved.json()["id"]
        self.assertFalse(saved.json()["recording_enabled"])
        self.assertEqual(self.stored_stream(camera_id), "rtsp://127.0.0.1/")  # A stand-in with no login
        shown = self.admin.get(f"/api/cameras/{camera_id}/capabilities").json()["capabilities"]
        self.assertEqual((shown["video"]["state"], shown["discovery"]["state"], shown["authentication"]["state"]), ("failed", "ok", "ok"))
        self.assertTrue(shown["video"]["message"] and PASSWORD not in shown["video"]["message"])  # The reason ONVIF gave, never the login
        # The camera is fixed; the recheck asks ONVIF again and stores the real address.
        broken.behaviours.clear()
        with mock.patch("app.recording.probe_stream", new=AsyncMock(return_value={"reachable": True, "video": {"codec": "h264", "width": 1920, "height": 1080}})):
            after = self.admin.post(f"/api/cameras/{camera_id}/capabilities/recheck").json()["capabilities"]
        self.assertEqual(after["video"]["state"], "ok")
        self.assertTrue(self.stored_stream(camera_id).startswith("rtsp://"))
        self.assertIn("/profile2/", self.stored_stream(camera_id))
        self.assertNotIn(PASSWORD, self.admin.get(f"/api/cameras/{camera_id}/capabilities").text)

    def test_a_camera_can_be_added_by_host_name(self):
        url = self.camera.url.replace("127.0.0.1", "localhost")
        with mock.patch("app.api.cameras.probe_stream", new=AsyncMock(return_value={"reachable": True, "video": {"codec": "h264", "width": 1920, "height": 1080}})):
            profiles = self.admin.post("/api/cameras/onvif-profiles", json={"camera_ip": "Localhost", "camera_username": USER, "camera_password": PASSWORD, "device_service_url": url})
            saved = self.admin.post("/api/cameras", json=self.body(camera_ip=" LocalHost ", device_service_url=url))
            wrong_host = self.admin.post("/api/cameras", json=self.body(name="Elsewhere", camera_ip="other.example.test", device_service_url=url))
        self.assertEqual(profiles.status_code, 200, profiles.text)
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(saved.json()["camera_ip"], "localhost")
        self.assertEqual(wrong_host.status_code, 422)  # The device address must be on the camera's own host
        self.assertIn("must match the camera address", wrong_host.json()["detail"])

    def test_an_unreachable_stream_with_a_live_substream_asked_for_saves_without_the_substream_or_its_token(self):
        with mock.patch("app.api.cameras.probe_stream", new=AsyncMock(return_value={"reachable": False, "reason": "No answer"})):
            saved = self.admin.post("/api/cameras", json=self.body(save_anyway=True, onvif_profile_token="profile2", live_substream_profile_token="profile3"))
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertIsNone(saved.json()["live_substream_profile_token"])

    def test_audio_is_not_called_unsupported_when_the_video_never_opened(self):
        with mock.patch("app.api.cameras.probe_stream", new=AsyncMock(return_value={"reachable": False, "reason": "No answer"})):
            saved = self.admin.post("/api/cameras", json={"name": "Direct", "stream_url": "rtsp://192.0.2.50/stream", "save_anyway": True})
        self.assertEqual(saved.status_code, 201, saved.text)
        shown = self.admin.get(f"/api/cameras/{saved.json()['id']}/capabilities").json()["capabilities"]
        self.assertEqual((shown["audio"]["state"], shown["discovery"]["state"], shown["authentication"]["state"]), ("unknown", "unsupported", "unknown"))

    def test_an_unknown_camera_is_not_found(self):
        missing = str(uuid.uuid4())
        self.assertEqual(self.admin.get(f"/api/cameras/{missing}/capabilities").status_code, 404)
        self.assertEqual(self.admin.post(f"/api/cameras/{missing}/capabilities/recheck").status_code, 404)


if __name__ == "__main__":
    unittest.main()
