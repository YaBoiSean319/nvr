"""A camera's advertised PTZ and Media2 addresses never receive the NVR's credentials unless they are on the configured host (#300)."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import httpx

from app import onvif_masks, onvif_ptz
from app.onvif_destination import UnsafeDestination, confine
from app.onvif_stream import DEVICE, MEDIA

CAMERA = "http://192.168.1.50/onvif/device_service"
EVIL = "http://127.0.0.1:9999/steal"


def soap(body: str) -> httpx.Response:
    return httpx.Response(200, content=(f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>{body}</s:Body></s:Envelope>').encode())


class Camera:
    """A camera whose capabilities advertise ``ptz``/``media`` addresses; records every request by host."""

    def __init__(self, ptz: str, media: str) -> None:
        self.ptz, self.media, self.seen = ptz, media, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.url.host, request.url.port, request.url.path, "authorization" in request.headers))
        body = request.content.decode()
        if "GetCapabilities" in body:
            return soap(f'<tds:Capabilities xmlns:tds="{DEVICE}"><tt:PTZ xmlns:tt="x"><tt:XAddr>{self.ptz}</tt:XAddr></tt:PTZ>'
                        f'<tt:Media xmlns:tt="x"><tt:XAddr>{self.media}</tt:XAddr></tt:Media></tds:Capabilities>')
        if "GetProfiles" in body:
            return soap(f'<trt:GetProfilesResponse xmlns:trt="{MEDIA}"><trt:Profiles token="p1"/></trt:GetProfilesResponse>')
        return soap("<ok/>")


def run(coro):
    return asyncio.run(coro)


class ConfineTests(unittest.TestCase):
    def test_moved_onto_the_configured_host_keeping_port_and_path(self):
        self.assertEqual(confine(CAMERA, "http://10.9.9.9:8080/onvif/ptz"), "http://192.168.1.50:8080/onvif/ptz")

    def test_credentials_and_odd_schemes_are_refused(self):
        for bad in ("http://user:pw@10.0.0.1/x", "file:///etc/passwd", "gopher://x/", "http:///x", "http://h:99999/x"):
            with self.assertRaises(UnsafeDestination, msg=bad):
                confine(CAMERA, bad)

    def test_no_downgrade_from_https(self):
        with self.assertRaises(UnsafeDestination):
            confine("https://cam.local/onvif", "http://cam.local/ptz")
        self.assertEqual(confine("https://cam.local/onvif", "https://other/ptz"), "https://cam.local/ptz")


class PtzDestinationTests(unittest.TestCase):
    def setUp(self):
        onvif_ptz._discovered.clear()

    def client(self, camera: Camera) -> onvif_ptz.PtzClient:
        ptz = onvif_ptz.PtzClient(CAMERA, "onvif", "pass", None)
        ptz._client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(camera))  # type: ignore[method-assign]
        return ptz

    def test_advertised_loopback_host_receives_nothing(self):
        camera = Camera(EVIL, "http://169.254.169.254/latest")
        run(self.client(camera).stop())
        self.assertTrue(camera.seen)
        self.assertEqual({host for host, *_ in camera.seen}, {"192.168.1.50"})
        self.assertIn(("192.168.1.50", 9999, "/steal", False), camera.seen)

    def test_legitimate_alternate_port_still_works(self):
        camera = Camera("http://192.168.1.50:8000/onvif/ptz", "http://192.168.1.50/onvif/media")
        run(self.client(camera).stop())
        self.assertEqual(camera.seen[-1][:3], ("192.168.1.50", 8000, "/onvif/ptz"))


class MaskDestinationTests(unittest.TestCase):
    def test_advertised_media2_host_receives_nothing(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            if "GetServices" in request.content.decode():
                return soap(f'<tds:GetServicesResponse xmlns:tds="{DEVICE}"><tds:Service><tds:Namespace>{onvif_masks.MEDIA2}</tds:Namespace>'
                            "<tds:XAddr>http://127.0.0.1:9999/media2</tds:XAddr></tds:Service></tds:GetServicesResponse>")
            return soap("<tr2:GetVideoSourceConfigurationsResponse xmlns:tr2='x'/>")

        real = httpx.AsyncClient
        with patch.object(onvif_masks.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            with self.assertRaises(onvif_masks.MasksUnsupported):
                run(onvif_masks.write_masks(CAMERA, "onvif", "pass", [{"x": 0, "y": 0, "width": .1, "height": .1}], []))
        self.assertEqual(set(seen), {"192.168.1.50"})


if __name__ == "__main__":
    unittest.main()
