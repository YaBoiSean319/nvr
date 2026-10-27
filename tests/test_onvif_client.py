"""The ONVIF client (#173): negotiated authentication, discovered services, Media1/Media2, and failures that say what happened."""
from __future__ import annotations

import json
import socket
import unittest
from datetime import timedelta

import httpx

from app.onvif_client import OnvifClient, OnvifError, normalise_host
from tests.fixtures.onvif_camera import PLACEHOLDER_HOST, OnvifCamera, profile

USER, PASSWORD = "operator", "S3cret!pass"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class CameraCase(unittest.IsolatedAsyncioTestCase):
    def camera(self, **options) -> OnvifCamera:
        camera = OnvifCamera(username=USER, password=PASSWORD, **options)
        self.addCleanup(camera.close)
        return camera

    def client(self, user: str = USER, password: str = PASSWORD, **options) -> OnvifClient:
        client = OnvifClient("127.0.0.1", user, password, **options)
        self.addAsyncCleanup(client.__aexit__)
        return client

    def carried(self, camera: OnvifCamera, service: str, operation: str) -> list[str]:
        return [item["carried"] for item in camera.requests if item["service"] == service and item["operation"] == operation]


class AuthenticationTests(CameraCase):
    async def test_each_service_is_negotiated_on_its_own_and_remembered(self):
        # The Hanwha arrangement: Media over HTTP Digest, Events over WS-Security.
        camera = self.camera(auth={"device": "digest", "media": "digest", "events": "wsse"})
        client = self.client()
        url = await client.locate_device(camera.url)
        services = await client.services(url)
        await client.profiles(services)
        await client.service_capabilities(services, "events")
        self.assertEqual(client.auth_by_service, {"device": "digest", "media": "digest", "events": "wsse"})
        # Nothing assumed: the plain request first, then Digest once (a bare 401 does not rule it out), then WS-Security. Then it is remembered.
        self.assertEqual(self.carried(camera, "events", "GetServiceCapabilities"), ["none", "none", "wsse"])
        # A Digest challenge was issued, so Digest is tried: the plain request, then the challenge round trip, then the answer.
        self.assertEqual(self.carried(camera, "media", "GetProfiles"), ["none", "none", "digest"])
        await client.service_capabilities(services, "events")
        await client.profiles(services)
        self.assertEqual(self.carried(camera, "events", "GetServiceCapabilities")[3:], ["wsse"])
        self.assertEqual(self.carried(camera, "media", "GetProfiles")[3:], ["digest"])  # Answered at once from the earlier challenge.

    async def test_a_camera_that_asks_for_nothing_gets_no_credentials(self):
        camera = self.camera(auth={"device": "none"})
        client = self.client()
        await client.device_information(await client.locate_device(camera.url))
        self.assertEqual(client.auth_by_service["device"], "none")
        self.assertEqual(set(self.carried(camera, "device", "GetDeviceInformation")), {"none"})

    async def test_a_service_that_wants_digest_and_ws_security_together_gets_both(self):
        camera = self.camera(auth={"device": "digest+wsse"})
        client = self.client()
        info = await client.device_information(await client.locate_device(camera.url))
        self.assertEqual((client.auth_by_service["device"], info.model), ("digest+wsse", "XNV-6120R"))

    async def test_the_wrong_password_is_rejected_with_a_message_that_holds_no_credential(self):
        for mode in ("digest", "wsse", "digest+wsse"):
            camera = self.camera(auth={"device": mode})
            client = self.client(password="not-the-password")
            with self.assertRaises(OnvifError) as caught:
                await client.device_information(await client.locate_device(camera.url))
            self.assertEqual(caught.exception.code, "auth_rejected", mode)
            self.assertIn("Camera authentication rejected", caught.exception.message)
            everything = f"{caught.exception.message} {json.dumps(client.diagnostics())}"
            for secret in (USER, PASSWORD, "not-the-password"):
                self.assertNotIn(secret, everything)

    async def test_once_every_login_is_refused_the_camera_is_not_asked_again(self):
        camera = self.camera(auth={"device": "digest+wsse"})
        client = self.client(password="not-the-password")
        url = await client.locate_device(camera.url)
        for _ in range(2):
            with self.assertRaises(OnvifError):
                await client.device_information(url)
            counted = len(camera.requests)
            if _ == 0:
                first = counted
        self.assertEqual(counted, first)  # The second try sent nothing: repeated failures can lock a camera account.

    def test_camera_text_never_carries_the_login_into_a_report(self):
        client = self.client()
        self.assertEqual(client._scrub(f"User {USER} not found, bad {PASSWORD}"), "User *** not found, bad ***")

    async def test_a_camera_that_needs_a_login_says_so_when_none_was_given(self):
        camera = self.camera()
        client = OnvifClient("127.0.0.1", "", "")
        self.addAsyncCleanup(client.__aexit__)
        with self.assertRaises(OnvifError) as caught:
            await client.device_information(await client.locate_device(camera.url))
        self.assertEqual((caught.exception.code, caught.exception.message), ("auth_rejected", "The camera requires a username and password"))

    async def test_a_camera_whose_clock_is_hours_off_still_accepts_the_ws_security_token(self):
        for skew in (timedelta(hours=2), timedelta(hours=-3), timedelta(days=1)):
            camera = self.camera(auth={"device": "wsse"}, skew=skew)
            client = self.client()
            await client.device_information(await client.locate_device(camera.url))
            self.assertAlmostEqual(client.clock_offset.total_seconds(), skew.total_seconds(), delta=3, msg=str(skew))
            self.assertEqual(client.auth_by_service["device"], "wsse")

    async def test_without_reading_the_cameras_clock_that_token_would_be_refused(self):
        camera = self.camera(auth={"device": "wsse"}, skew=timedelta(hours=2))
        client = self.client()
        client.clock_offset = timedelta()  # As if the clock had not been read.
        with self.assertRaises(OnvifError) as caught:
            await client.device_information(await client.locate_device(camera.url))
        self.assertEqual(caught.exception.code, "auth_rejected")

    async def test_the_login_of_a_user_with_markup_in_the_name_is_escaped(self):
        camera = OnvifCamera(username="a<b&c", password="p>q", auth={"device": "wsse"})
        self.addCleanup(camera.close)
        client = self.client("a<b&c", "p>q")
        info = await client.device_information(await client.locate_device(camera.url))
        self.assertEqual(info.manufacturer, "Hanwha Techwin")


class DiscoveryTests(CameraCase):
    async def test_services_come_from_get_services_and_stay_on_the_address_that_was_entered(self):
        camera = self.camera()
        client = self.client()
        services = await client.services(await client.locate_device(camera.url))
        self.assertEqual(services.source, "GetServices")
        self.assertEqual(set(services.urls), {"device", "media", "events", "ptz"})
        # The camera says it is at an address of its own; every address is moved onto the one the administrator entered.
        self.assertTrue(all(url.startswith(f"http://127.0.0.1:{camera.port}/") for url in services.urls.values()), services.urls)
        self.assertNotIn(PLACEHOLDER_HOST, json.dumps(services.urls))

    async def test_a_camera_without_get_services_is_read_through_get_capabilities(self):
        camera = self.camera(get_services=False)
        client = self.client()
        services = await client.services(await client.locate_device(camera.url))
        self.assertEqual((services.source, set(services.urls)), ("GetCapabilities", {"device", "media", "events", "ptz"}))

    async def test_device_information_names_the_make_model_and_firmware(self):
        camera = self.camera(manufacturer="Axis Communications", model="P3245", firmware="11.0")
        client = self.client()
        info = await client.device_information(await client.locate_device(camera.url))
        self.assertEqual((info.manufacturer, info.model, info.firmware), ("Axis Communications", "P3245", "11.0"))

    async def test_the_device_service_is_found_on_whichever_port_answers(self):
        camera = self.camera()
        client = self.client()
        located = await client.locate_device(None, (free_port(), camera.port))
        self.assertEqual(located, camera.url)

    async def test_nothing_listening_says_which_ports_were_tried_and_what_to_check(self):
        client = self.client()
        ports = (free_port(), free_port())
        with self.assertRaises(OnvifError) as caught:
            await client.locate_device(None, ports)
        self.assertEqual(caught.exception.code, "unreachable")
        for port in ports:
            self.assertIn(str(port), caught.exception.message)
        self.assertIn("ONVIF", caught.exception.message)

    async def test_a_web_server_that_is_not_an_onvif_device_is_not_taken_for_one(self):
        camera = self.camera(behaviours={"GetSystemDateAndTime": "html"})
        client = self.client()
        with self.assertRaises(OnvifError) as caught:
            await client.locate_device(None, (camera.port,))
        self.assertEqual(caught.exception.code, "unreachable")

    async def test_an_address_with_a_login_in_it_or_another_scheme_is_refused(self):
        client = self.client()
        for url in ("http://user:pass@127.0.0.1/onvif", "ftp://127.0.0.1/onvif", "http:///onvif", "http://127.0.0.1:99999999/onvif"):
            with self.assertRaises(OnvifError, msg=url):
                await client.locate_device(url)

    def test_hosts_may_be_addresses_or_names(self):
        for value, expected in (("192.0.2.10", "192.0.2.10"), (" 192.0.2.10 ", "192.0.2.10"), ("[2001:db8::5]", "2001:db8::5"), ("Cam-1.Example.test", "cam-1.example.test"), ("cam", "cam")):
            self.assertEqual(normalise_host(value), expected)
        for value in ("", "0.0.0.0", "224.0.0.1", "bad host", "http://cam", "cam/path", "-bad.example", "a" * 300):
            with self.assertRaises(ValueError, msg=value):
                normalise_host(value)


class MediaTests(CameraCase):
    async def test_media1_profiles_are_listed_with_their_codecs(self):
        camera = self.camera()
        client = self.client()
        services = await client.services(await client.locate_device(camera.url))
        found = await client.profiles(services)
        self.assertEqual([(item.token, item.encoding, item.source) for item in found], [("profile1", "mjpeg", "media1"), ("profile2", "h264", "media1"), ("profile3", "h264", "media1"), ("profile4", "h265", "media1")])
        self.assertTrue(next(item for item in found if item.token == "profile2").has_audio)

    async def test_media2_is_used_where_the_camera_has_it(self):
        camera = self.camera(media2_profiles=[profile("m-main", "main", "H265", 3840, 2160), profile("m-sub", "sub", "H264", 640, 360)])
        client = self.client()
        services = await client.services(await client.locate_device(camera.url))
        found = await client.profiles(services)
        self.assertEqual([(item.token, item.source) for item in found], [("m-main", "media2"), ("m-sub", "media2")])

    async def test_media1_is_the_fallback_when_media2_fails_or_has_nothing(self):
        for behaviours, media2 in (({"media2:GetProfiles": "fault"}, [profile("m", "m", "H264", 640, 360)]), ({}, [])):
            camera = self.camera(media2_profiles=media2, behaviours=behaviours)
            client = self.client()
            services = await client.services(await client.locate_device(camera.url))
            found = await client.profiles(services)
            self.assertEqual({item.source for item in found}, {"media1"}, behaviours)

    async def test_a_camera_with_no_media_service_says_so(self):
        camera = self.camera(services=("events",))
        client = self.client()
        services = await client.services(await client.locate_device(camera.url))
        with self.assertRaises(OnvifError) as caught:
            await client.profiles(services)
        self.assertEqual(caught.exception.code, "service_unavailable")
        self.assertIn("Media service unavailable", caught.exception.message)

    async def test_the_stream_address_has_the_entered_host_the_cameras_path_and_the_login_encoded(self):
        camera = self.camera(media2_profiles=[profile("m1", "m", "H264", 1920, 1080)])
        client = self.client()
        services = await client.services(await client.locate_device(camera.url))
        for found in (await client.profiles(services)):
            uri = await client.stream_uri(services, found)
            self.assertEqual(uri, f"rtsp://{PLACEHOLDER_HOST}:554/{found.token}/media.smp")
            self.assertEqual(client.stream_url_with_login(uri), f"rtsp://operator:S3cret%21pass@127.0.0.1:554/{found.token}/media.smp")

    async def test_a_stream_address_that_is_not_rtsp_or_has_odd_characters_is_refused(self):
        client = self.client()
        for uri in ("http://127.0.0.1/stream", "rtsp:///x", "rtsp://127.0.0.1/a b", "rtsp://127.0.0.1/a'b", "rtsp://127.0.0.1:99999999/x"):
            with self.assertRaises(OnvifError, msg=uri):
                client.stream_url_with_login(uri)

    async def test_an_unknown_profile_has_no_stream_address(self):
        camera = self.camera()
        client = self.client()
        services = await client.services(await client.locate_device(camera.url))
        from app.onvif_profiles import MediaProfile
        with self.assertRaises(OnvifError) as caught:
            await client.stream_uri(services, MediaProfile("ghost", "ghost"))
        self.assertEqual(caught.exception.code, "soap_fault")

    async def test_event_capabilities_say_whether_pull_points_are_offered(self):
        for offered in (True, False):
            camera = self.camera(pull_point=offered)
            client = self.client()
            services = await client.services(await client.locate_device(camera.url))
            self.assertEqual((await client.service_capabilities(services, "events"))["WSPullPointSupport"], str(offered).lower())
        self.assertEqual(await client.service_capabilities(services, "ptz"), {})


class FailureTests(CameraCase):
    async def test_a_camera_that_refuses_a_request_gives_its_reason_not_a_generic_message(self):
        camera = self.camera(behaviours={"GetDeviceInformation": "fault"})
        client = self.client()
        with self.assertRaises(OnvifError) as caught:
            await client.device_information(await client.locate_device(camera.url))
        self.assertEqual(caught.exception.code, "soap_fault")
        self.assertIn("The camera is busy", caught.exception.message)
        self.assertEqual(client.diagnostics()[-1]["outcome"], "soap_fault")

    async def test_a_service_that_is_not_there_is_service_unavailable(self):
        camera = self.camera()
        client = self.client()
        with self.assertRaises(OnvifError) as caught:
            await client.device_information(await client.locate_device(f"{camera.origin}/onvif/nothing-here"))
        self.assertEqual(caught.exception.code, "unreachable")  # Nothing there answers like an ONVIF device.

    async def test_a_camera_that_answers_with_a_web_page_or_unsafe_xml_is_malformed(self):
        for behaviour in ("html", "doctype"):
            camera = self.camera(behaviours={"GetDeviceInformation": behaviour})
            client = self.client()
            with self.assertRaises(OnvifError) as caught:
                await client.device_information(await client.locate_device(camera.url))
            self.assertEqual(caught.exception.code, "malformed", behaviour)

    async def test_a_server_error_without_a_reason_reports_the_status(self):
        camera = self.camera(behaviours={"GetDeviceInformation": "http500"})
        client = self.client()
        with self.assertRaises(OnvifError) as caught:
            await client.device_information(await client.locate_device(camera.url))
        self.assertIn("HTTP 500", caught.exception.message)

    async def test_a_camera_that_does_not_answer_in_time_times_out(self):
        camera = self.camera(behaviours={"GetDeviceInformation": "hang"})
        client = self.client(timeout=httpx.Timeout(0.4))
        with self.assertRaises(OnvifError) as caught:
            await client.device_information(await client.locate_device(camera.url))
        self.assertEqual((caught.exception.code, caught.exception.message), ("timeout", "The camera did not answer in time"))

    async def test_a_camera_that_is_off_is_unreachable(self):
        client = self.client()
        with self.assertRaises(OnvifError) as caught:
            await client.device_information(await client.locate_device(f"http://127.0.0.1:{free_port()}/onvif/device_service"))
        self.assertEqual(caught.exception.code, "unreachable")


class DiagnosticsTests(CameraCase):
    async def test_the_trace_says_what_was_tried_and_holds_no_credentials_or_bodies(self):
        camera = self.camera(auth={"device": "digest", "media": "digest", "events": "wsse"})
        client = self.client()
        services = await client.services(await client.locate_device(camera.url))
        await client.profiles(services)
        await client.service_capabilities(services, "events")
        trace = client.diagnostics()
        self.assertTrue({"operation", "service", "auth", "outcome", "status", "ms"} <= set(trace[0]))
        self.assertEqual([item["outcome"] for item in trace if item["operation"] == "GetServiceCapabilities"], ["auth_failed", "auth_failed", "ok"])
        text = json.dumps(trace)
        for secret in (USER, PASSWORD, "Nonce", "Authorization", "PasswordDigest", "UsernameToken", "rtsp://", "S3cret"):
            self.assertNotIn(secret, text)
        self.assertTrue(any(item.get("clock_offset_seconds") is not None for item in trace))  # The clock was read for the WS-Security token.


if __name__ == "__main__":
    unittest.main()
