"""Inspecting a camera from its address and login (#173): the steps, the capabilities judged separately, real video decoding, and the API."""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import camera_inspect
from app.onvif_stream import list_camera_stream_profiles, resolve_camera_stream
from tests.fixtures import detection_scene as scene
from tests.fixtures.onvif_camera import OnvifCamera, profile
from tests.test_access_grants import GrantCase

USER, PASSWORD = "operator", "S3cret!pass"
DECODED = {"decoded": True, "opened": True}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class InspectCase(unittest.IsolatedAsyncioTestCase):
    def camera(self, **options) -> OnvifCamera:
        options.setdefault("auth", {"device": "digest", "media": "digest", "events": "wsse"})
        camera = OnvifCamera(username=USER, password=PASSWORD, **options)
        self.addCleanup(camera.close)
        return camera

    async def inspect(self, camera: OnvifCamera, *, password: str = PASSWORD, verify=DECODED, **options) -> dict:
        with mock.patch("app.camera_inspect.verify_video", new=mock.AsyncMock(return_value=verify)) as checked:
            result = await camera_inspect.inspect_camera("127.0.0.1", USER, password, device_service_url=camera.url, **options)
        self.verified = checked
        return result

    @staticmethod
    def states(result: dict) -> dict[str, str]:
        return {step["id"]: step["state"] for step in result["steps"]}


class HanwhaTests(InspectCase):
    async def test_a_camera_is_added_from_its_login_alone_and_everything_is_found(self):
        result = await self.inspect(self.camera())
        self.assertTrue(result["ok"])
        self.assertEqual(result["device"], {"manufacturer": "Hanwha Techwin", "model": "XNV-6120R", "firmware": "2.00.00"})
        self.assertEqual(result["brand"], "hanwha")
        self.assertEqual([step["label"] for step in result["steps"]], ["Connecting to camera", "Authenticating", "Identifying device", "Discovering streams", "Configuring motion events", "Verifying video", "Camera ready"])
        self.assertEqual(set(self.states(result).values()), {"ok"})
        self.assertEqual(result["auth"], {"device": "digest", "media": "digest", "events": "wsse"})
        # The H.264 main stream is chosen to record, not the MJPEG profile the camera lists first and not the H.265 one; the H.264 sub-stream is for the grid.
        self.assertEqual(result["selection"]["record_token"], "profile2")
        self.assertEqual(result["selection"]["live_token"], "profile3")
        self.assertIn("MJPEG", result["selection"]["reason"])
        self.assertEqual([item["token"] for item in result["profiles"]], ["profile1", "profile2", "profile3", "profile4"])
        capabilities = {name: item["state"] for name, item in result["capabilities"].items()}
        self.assertEqual(capabilities, {"discovery": "ok", "authentication": "ok", "video": "ok", "audio": "available", "motion": "ok", "ptz": "available"})
        # The main stream and the live sub-stream are both decoded.
        self.assertEqual(self.verified.await_count, 2)
        self.assertIs(result["selection"]["live_verified"], True)

    async def test_a_camera_that_checks_the_events_action_is_inspected_with_motion_working(self):
        result = await self.inspect(self.camera(strict_actions=True))
        self.assertEqual(result["capabilities"]["motion"]["state"], "ok", result["capabilities"]["motion"])

    async def test_a_live_substream_that_cannot_be_decoded_is_a_warning_and_video_stays_ok(self):
        results = [DECODED, {"decoded": False, "opened": True, "reason": "RTSP stream connected but video decoding failed"}]
        with mock.patch("app.camera_inspect.verify_video", new=mock.AsyncMock(side_effect=results)):
            result = await camera_inspect.inspect_camera("127.0.0.1", USER, PASSWORD, device_service_url=self.camera().url)
        self.assertEqual((result["ok"], result["selection"]["live_verified"]), (True, False))
        self.assertTrue(any("live sub-stream could not be verified" in item for item in result["selection"]["warnings"]))

    async def test_the_report_holds_no_credential_and_no_credentialed_address(self):
        result = await self.inspect(self.camera())
        text = json.dumps(result)
        for secret in (USER, PASSWORD, "S3cret", "rtsp://", "Nonce", "PasswordDigest"):
            self.assertNotIn(secret, text)
        # The verified stream carries the login, in memory only, and the camera's own host is replaced by the one entered.
        sources = [call.args[0] for call in self.verified.await_args_list]
        self.assertEqual(sources, ["rtsp://operator:S3cret%21pass@127.0.0.1:554/profile2/media.smp", "rtsp://operator:S3cret%21pass@127.0.0.1:554/profile3/media.smp"])

    async def test_video_that_does_not_decode_fails_the_video_step_but_not_the_other_capabilities(self):
        result = await self.inspect(self.camera(), verify={"decoded": False, "opened": True, "reason": "RTSP stream connected but video decoding failed"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["capabilities"]["video"], {"state": "failed", "message": "RTSP stream connected but video decoding failed"})
        # Motion, audio and PTZ are judged on their own: a failed picture does not hide them.
        self.assertEqual({name: result["capabilities"][name]["state"] for name in ("authentication", "motion", "audio", "ptz")}, {"authentication": "ok", "motion": "ok", "audio": "available", "ptz": "available"})
        steps = {step["id"]: step for step in result["steps"]}
        self.assertEqual((steps["video"]["state"], steps["ready"]["state"]), ("failed", "failed"))
        self.assertIn("can still be saved", steps["ready"]["detail"])

    async def test_a_stream_that_will_not_open_says_so(self):
        reason = "The camera's video stream could not be opened: Camera stream could not be opened"
        result = await self.inspect(self.camera(), verify={"decoded": False, "opened": False, "reason": reason})
        self.assertEqual(result["capabilities"]["video"]["message"], reason)

    async def test_verification_can_be_skipped_and_then_video_is_not_called_healthy(self):
        camera = self.camera()
        with mock.patch("app.camera_inspect.verify_video", new=mock.AsyncMock()) as checked:
            result = await camera_inspect.inspect_camera("127.0.0.1", USER, PASSWORD, device_service_url=camera.url, verify=False)
        checked.assert_not_awaited()
        self.assertEqual((result["ok"], result["capabilities"]["video"]["state"]), (True, "unknown"))


class FailureTests(InspectCase):
    async def test_a_rejected_login_stops_at_authentication_with_an_actionable_message(self):
        result = await self.inspect(self.camera(), password="not-the-password")
        self.assertFalse(result["ok"])
        self.assertEqual(self.states(result), {"connect": "ok", "authenticate": "failed", "identify": "skipped", "streams": "skipped", "motion": "skipped", "video": "skipped", "ready": "skipped"})
        self.assertIn("Camera authentication rejected", result["steps"][1]["detail"])
        self.assertEqual(result["capabilities"]["authentication"]["state"], "failed")
        for secret in (USER, PASSWORD, "not-the-password"):
            self.assertNotIn(secret, json.dumps(result))

    async def test_a_camera_that_is_not_there_stops_at_connecting(self):
        result = await camera_inspect.inspect_camera("127.0.0.1", USER, PASSWORD, device_service_url=f"http://127.0.0.1:{free_port()}/onvif/device_service", verify=False)
        self.assertEqual(self.states(result)["connect"], "failed")
        self.assertEqual(result["capabilities"]["discovery"]["state"], "failed")
        self.assertIn("No ONVIF service answered", result["steps"][0]["detail"])

    async def test_an_address_that_is_not_one_is_refused_in_the_first_step(self):
        result = await camera_inspect.inspect_camera("not a host!", USER, PASSWORD, verify=False)
        self.assertEqual((self.states(result)["connect"], result["steps"][0]["detail"]), ("failed", "Enter the camera's IP address or host name"))

    async def test_no_media_service_fails_video_but_motion_events_are_still_found(self):
        # The two are independent: a camera whose Media service is missing still offers ONVIF motion events.
        result = await self.inspect(self.camera(services=("events",)))
        self.assertFalse(result["ok"])
        self.assertEqual(result["capabilities"]["video"]["state"], "failed")
        self.assertIn("Media service unavailable", result["capabilities"]["video"]["message"])
        self.assertEqual(result["capabilities"]["motion"]["state"], "ok")
        self.assertEqual(self.verified.await_count, 0)

    async def test_a_camera_with_only_mjpeg_is_used_with_a_warning(self):
        result = await self.inspect(self.camera(profiles=[profile("only", "mjpeg", "JPEG", 1280, 720)]))
        self.assertEqual(result["selection"]["record_token"], "only")
        self.assertTrue(any("MJPEG" in warning for warning in result["selection"]["warnings"]))

    async def test_a_camera_with_no_usable_codec_has_no_video_profile(self):
        result = await self.inspect(self.camera(profiles=[profile("odd", "odd", "MPEG4", 640, 480)]))
        self.assertEqual(result["capabilities"]["video"], {"state": "failed", "message": "No compatible video profile found"})
        self.assertEqual(self.states(result)["streams"], "failed")

    async def test_motion_is_unsupported_without_events_or_without_pull_points(self):
        for options, detail in (({"services": ("media",)}, "Events service"), ({"pull_point": False}, "pull-point")):
            result = await self.inspect(self.camera(**options))
            self.assertEqual(result["capabilities"]["motion"]["state"], "unsupported", options)
            self.assertEqual(self.states(result)["motion"], "skipped")
            self.assertIn(detail, result["steps"][4]["detail"] + result["capabilities"]["motion"]["message"])
            self.assertTrue(result["ok"])  # Motion is not needed for video.

    async def test_a_camera_that_will_not_describe_itself_can_still_be_used(self):
        result = await self.inspect(self.camera(behaviours={"GetDeviceInformation": "fault"}))
        self.assertEqual(self.states(result)["identify"], "failed")
        self.assertEqual((result["device"], result["brand"], result["ok"]), ({}, "auto", True))

    def test_brands_come_from_the_manufacturer_name(self):
        for name, brand in (("Hanwha Techwin", "hanwha"), ("Samsung Techwin", "samsung"), ("WISENET", "hanwha"), ("AXIS Communications", "axis"), ("Hikvision", "hikvision"), ("Somebody Else", "other"), ("", "auto")):
            self.assertEqual(camera_inspect.brand_of(name), brand, name)

    async def test_inspections_run_a_few_at_a_time(self):
        camera = self.camera()
        running = peak = 0

        async def slow(source, protocols=None):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return DECODED
        with mock.patch("app.camera_inspect.verify_video", new=slow):
            results = await asyncio.gather(*(camera_inspect.inspect_camera("127.0.0.1", USER, PASSWORD, device_service_url=camera.url) for _ in range(12)))
        self.assertTrue(all(item["ok"] for item in results))
        self.assertLessEqual(peak, camera_inspect.MAX_CONCURRENT_INSPECTIONS)
        self.assertGreater(peak, 1)


class DecodeTests(unittest.IsolatedAsyncioTestCase):
    FILES = "file,concat,pipe,tcp,udp,rtp"

    async def test_a_stream_that_decodes_is_verified_by_decoding_a_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            footage = scene.write_footage(Path(directory) / "scene.mp4")
            result = await camera_inspect.verify_video(f"file:{footage}", self.FILES)
        self.assertTrue(result["decoded"])

    async def test_a_stream_that_connects_but_cannot_be_decoded_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / "broken.mp4"
            broken.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"not video" * 100)
            result = await camera_inspect.verify_video(f"file:{broken}", self.FILES)
        self.assertEqual((result["decoded"], result["reason"]), (False, "RTSP stream connected but video decoding failed"))

    async def test_a_stream_with_no_video_frames_is_not_called_decoded(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.wav"
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", str(audio)], check=True)
            result = await camera_inspect.verify_video(f"file:{audio}", self.FILES)
        self.assertFalse(result["decoded"])

    async def test_a_stream_that_never_answers_times_out_with_a_reason(self):
        with mock.patch.object(camera_inspect, "DECODE_TIMEOUT_SECONDS", 0.3), mock.patch("app.recording._manifest_bytes", return_value=b"ffconcat version 1.0\nfile /dev/zero\n"):
            result = await camera_inspect.verify_video("file:/dev/zero", self.FILES)
        self.assertFalse(result["decoded"])

    async def test_without_ffmpeg_verification_says_it_is_unavailable(self):
        with mock.patch("app.camera_inspect.shutil.which", return_value=None):
            result = await camera_inspect.verify_video("rtsp://127.0.0.1/x")
        self.assertFalse(result["decoded"])
        self.assertIn("FFmpeg", result["reason"])


class OnboardingApiTests(GrantCase):
    def setUp(self):
        super().setUp()
        self.camera = OnvifCamera(username=USER, password=PASSWORD, auth={"device": "digest", "media": "digest", "events": "wsse"})
        self.addCleanup(self.camera.close)
        patcher = mock.patch("app.camera_inspect.verify_video", new=mock.AsyncMock(return_value=DECODED))
        patcher.start()
        self.addCleanup(patcher.stop)

    def body(self, **extra) -> dict:
        return {"host": "127.0.0.1", "username": USER, "password": PASSWORD, "device_service_url": self.camera.url, **extra}

    def test_an_administrator_inspects_a_camera_from_its_login(self):
        response = self.admin.post("/api/cameras/inspect", json=self.body())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["ok"])
        self.assertEqual((result["device"]["model"], result["selection"]["record_token"]), ("XNV-6120R", "profile2"))
        for secret in (USER, PASSWORD, "rtsp://"):
            self.assertNotIn(secret, response.text)
        (entry,) = self.audit("camera_inspected")
        self.assertEqual((entry["resource_id"], entry["details"]), ("127.0.0.1", {"ok": True, "failed": []}))
        self.assertNotIn(PASSWORD, json.dumps(entry))

    def test_a_failed_inspection_is_still_a_report_with_the_reason(self):
        response = self.admin.post("/api/cameras/inspect", json=self.body(password="wrong-password"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertIn("authentication rejected", response.json()["steps"][1]["detail"])
        self.assertEqual(self.audit("camera_inspected")[0]["details"], {"ok": False, "failed": ["authenticate"]})

    def test_the_request_is_validated_and_only_administrators_may_make_it(self):
        for body in ({"host": ""}, {"host": "bad host"}, {"host": "127.0.0.1", "username": USER}, {"host": "127.0.0.1", "password": PASSWORD}, {"host": "127.0.0.1", "camera_brand": "nonesuch"}):
            self.assertEqual(self.admin.post("/api/cameras/inspect", json=body).status_code, 422, body)
        self.assertEqual(self.limited.post("/api/cameras/inspect", json=self.body()).status_code, 403)
        from fastapi.testclient import TestClient
        self.assertEqual(TestClient(self.app).post("/api/cameras/inspect", json=self.body()).status_code, 401)

    def test_a_host_name_is_accepted(self):
        response = self.admin.post("/api/cameras/inspect", json={"host": "Camera-1.Example.test", "verify_video": False, "device_service_url": "http://camera-1.example.test:9/onvif/device_service"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["steps"][0]["state"], "failed")  # Nothing there, but the name was accepted.

    def test_the_existing_profile_list_now_carries_codecs_and_the_profiles_the_nvr_would_choose(self):
        response = self.admin.post("/api/cameras/onvif-profiles", json={"camera_ip": "127.0.0.1", "camera_username": USER, "camera_password": PASSWORD, "device_service_url": self.camera.url})
        self.assertEqual(response.status_code, 200, response.text)
        profiles = {item["token"]: item for item in response.json()["profiles"]}
        self.assertEqual((profiles["profile1"]["encoding"], profiles["profile2"]["encoding"], profiles["profile4"]["encoding"]), ("mjpeg", "h264", "h265"))
        self.assertEqual({token: item["suggested"] for token, item in profiles.items()}, {"profile1": "", "profile2": "record", "profile3": "live", "profile4": ""})
        self.assertEqual((profiles["profile2"]["width"], profiles["profile2"]["height"]), (1920, 1080))
        self.assertNotIn(PASSWORD, response.text)

    def test_the_existing_profile_list_says_why_when_the_login_is_wrong(self):
        response = self.admin.post("/api/cameras/onvif-profiles", json={"camera_ip": "127.0.0.1", "camera_username": USER, "camera_password": "wrong-password", "device_service_url": self.camera.url})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Camera authentication rejected", response.json()["detail"])
        self.assertNotIn("Could not retrieve ONVIF stream profiles", response.text)


class ResolveTests(InspectCase):
    async def test_the_default_stream_is_the_h264_main_not_the_first_profile_which_is_mjpeg(self):
        camera = self.camera()
        source = await resolve_camera_stream("127.0.0.1", USER, PASSWORD, camera.url)
        self.assertEqual(source.stream_url, "rtsp://operator:S3cret%21pass@127.0.0.1:554/profile2/media.smp")
        self.assertEqual(source.device_service_url, camera.url)

    async def test_a_chosen_profile_is_honoured_and_a_missing_one_is_an_error_not_a_silent_change(self):
        camera = self.camera()
        source = await resolve_camera_stream("127.0.0.1", USER, PASSWORD, camera.url, "profile3")
        self.assertTrue(source.stream_url.endswith("/profile3/media.smp"))
        with self.assertRaisesRegex(ValueError, "not on the camera any more"):
            await resolve_camera_stream("127.0.0.1", USER, PASSWORD, camera.url, "gone")

    async def test_failures_are_actionable_value_errors(self):
        camera = self.camera()
        with self.assertRaisesRegex(ValueError, "Camera authentication rejected"):
            await resolve_camera_stream("127.0.0.1", USER, "wrong", camera.url)
        with self.assertRaisesRegex(ValueError, "Media service unavailable"):
            await resolve_camera_stream("127.0.0.1", USER, PASSWORD, self.camera(services=("events",)).url)
        with self.assertRaisesRegex(ValueError, "No compatible video profile"):
            await resolve_camera_stream("127.0.0.1", USER, PASSWORD, self.camera(profiles=[profile("odd", "odd", "MPEG4", 640, 480)]).url)

    async def test_profiles_are_listed_for_a_camera_that_wants_ws_security_for_media(self):
        camera = self.camera(auth={"device": "wsse", "media": "wsse"})
        url, profiles = await list_camera_stream_profiles("127.0.0.1", USER, PASSWORD, camera.url)
        self.assertEqual((url, [item.token for item in profiles]), (camera.url, ["profile1", "profile2", "profile3", "profile4"]))


if __name__ == "__main__":
    unittest.main()
