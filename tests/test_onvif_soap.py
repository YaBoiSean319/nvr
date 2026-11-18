"""What a camera sends back is read the same careful way wherever it is read, and what is sent to it is escaped (code health).

Every reader of camera XML refuses a document that declares a DOCTYPE or an entity, an empty answer and one over its own limit; the PTZ, privacy-mask
and relay calls send the same envelope and report a failed call the same way. These were written out once per module. The tests pin what each caller
accepts, refuses and sends, so that keeping the shared parts in one place cannot change any of it."""
from __future__ import annotations

import asyncio
import ipaddress
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import httpx

from app import discovery, onvif_client, onvif_events, onvif_masks, onvif_ptz, onvif_relay, onvif_stream
from app.onvif_client import OnvifError

CAMERA = "http://192.0.2.50/onvif/device_service"
UNSAFE = (
    b"<!DOCTYPE x><x/>",
    b"<!doctype x><x/>",
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>',
    b'<x><!EnTiTy a "b"></x>',
)
PROBE_MATCH = (
    b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    b'xmlns:a="http://www.w3.org/2005/08/addressing"><s:Body><d:ProbeMatches><d:ProbeMatch><a:EndpointReference>'
    b"<a:Address>urn:uuid:5a30e8d2-1108-4c1e-9b57-76a30b1d8087</a:Address></a:EndpointReference>"
    b"<d:XAddrs>http://192.0.2.50/onvif/device_service</d:XAddrs></d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"
)
DEVICE_INFORMATION = (b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
                      b"<s:Body><tds:GetDeviceInformationResponse/></s:Body></s:Envelope>")


def padded(document: bytes, size: int) -> bytes:
    """The document made exactly ``size`` bytes long with a trailing comment, which changes nothing a reader sees."""
    filler = size - len(document) - len(b"<!---->")
    assert filler >= 0
    return document + b"<!--" + b" " * filler + b"-->"


def soap(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>{body}</s:Body></s:Envelope>'.encode())


def run(coro):
    return asyncio.run(coro)


class Recorder:
    """Stands in for the camera: keeps each request's content type and body, and answers with ``answer`` (a response, or a function of the body)."""

    def __init__(self, answer=None) -> None:
        self.answer, self.seen = answer, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        self.seen.append((request.headers["content-type"], body))
        if callable(self.answer):
            return self.answer(body)
        return self.answer if self.answer is not None else soap("<ok/>")


def camera(module, handler):
    """Every HTTP client the module opens talks to ``handler`` instead of the network."""
    real = httpx.AsyncClient
    return patch.object(module.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


class ReaderTests(unittest.TestCase):
    def test_every_reader_refuses_a_doctype_or_an_entity_in_any_case(self):
        for data in UNSAFE:
            with self.assertRaises(OnvifError) as caught:
                onvif_client.parse_xml(data)
            self.assertEqual(caught.exception.code, "malformed", data)
            with self.assertRaisesRegex(ValueError, "ONVIF response XML is unsafe"):
                onvif_events.parse_motion_notifications(data)
            self.assertEqual(discovery.parse_probe_response(data), [], data)

    def test_every_reader_refuses_an_empty_answer(self):
        with self.assertRaises(OnvifError) as caught:
            onvif_client.parse_xml(b"")
        self.assertEqual(caught.exception.code, "malformed")
        with self.assertRaisesRegex(ValueError, "ONVIF response size is invalid"):
            onvif_events.parse_motion_notifications(b"")
        self.assertEqual(discovery.parse_probe_response(b""), [])

    def test_each_reader_keeps_its_own_size_limit(self):
        limit = onvif_client.MAX_RESPONSE_BYTES
        self.assertEqual(limit, 2 * 1024 * 1024)
        self.assertEqual(onvif_client.parse_xml(padded(b"<x/>", limit)).tag, "x")
        with self.assertRaises(OnvifError) as caught:
            onvif_client.parse_xml(padded(b"<x/>", limit + 1))
        self.assertEqual(caught.exception.code, "malformed")

        limit = onvif_events.MAX_XML_BYTES
        self.assertEqual(limit, 1024 * 1024)
        self.assertEqual(onvif_events.parse_motion_notifications(padded(b"<x/>", limit)), [])
        with self.assertRaisesRegex(ValueError, "ONVIF response size is invalid"):
            onvif_events.parse_motion_notifications(padded(b"<x/>", limit + 1))

        limit = discovery.MAX_DATAGRAM_BYTES
        self.assertEqual(limit, 64 * 1024)
        self.assertEqual(len(discovery.parse_probe_response(padded(PROBE_MATCH, limit))), 1)
        self.assertEqual(discovery.parse_probe_response(padded(PROBE_MATCH, limit + 1)), [])

    def test_broken_xml_is_reported_the_way_each_caller_expects(self):
        with self.assertRaises(OnvifError) as caught:
            onvif_client.parse_xml(b"<broken")
        self.assertEqual(caught.exception.code, "malformed")
        with self.assertRaises(ET.ParseError):
            onvif_events.parse_motion_notifications(b"<broken")
        self.assertEqual(discovery.parse_probe_response(b"<broken"), [])

    def test_element_names_are_read_without_their_namespace_in_every_module(self):
        for module in (onvif_client, onvif_events, onvif_ptz, onvif_masks, onvif_relay):
            self.assertEqual(module._local("{http://www.onvif.org/ver10/schema}Profiles"), "Profiles", module.__name__)
            self.assertEqual(module._local("Profiles"), "Profiles", module.__name__)


class RangeProbeTests(unittest.IsolatedAsyncioTestCase):
    """The address-range scan asks each address for its device information over a plain socket and reads the answer itself."""

    async def probe(self, status_line: bytes, body: bytes):
        async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readuntil(b"</s:Envelope>")
            writer.write(status_line + b"\r\nContent-Type: application/soap+xml\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(answer, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            return await discovery._probe_onvif_endpoint(ipaddress.IPv4Address("127.0.0.1"), port, asyncio.Semaphore(1))
        finally:
            server.close()
            await server.wait_closed()

    async def test_a_device_answer_is_a_camera_and_the_same_answer_with_a_doctype_is_not(self):
        found = await self.probe(b"HTTP/1.1 200 OK", DEVICE_INFORMATION)
        self.assertIsNotNone(found)
        self.assertEqual(len(found.service_urls), 1)
        self.assertTrue(found.service_urls[0].endswith("/onvif/device_service"))
        self.assertIsNone(await self.probe(b"HTTP/1.1 200 OK", b"<!DOCTYPE x>" + DEVICE_INFORMATION))
        self.assertIsNone(await self.probe(b"HTTP/1.1 200 OK", b'<!doctype x [<!entity a "b">]>' + DEVICE_INFORMATION))

    async def test_a_page_that_is_not_onvif_is_not_a_camera_and_a_login_demand_is(self):
        self.assertIsNone(await self.probe(b"HTTP/1.1 200 OK", b"<html><body>Router</body></html>"))
        self.assertIsNone(await self.probe(b"HTTP/1.1 200 OK", b"<broken"))
        self.assertIsNotNone(await self.probe(b"HTTP/1.1 401 Unauthorized", b""))


class RelayCallTests(unittest.TestCase):
    def test_the_relay_is_switched_on_then_off_with_the_token_escaped(self):
        seen = Recorder()
        with camera(onvif_relay, seen):
            self.assertEqual(run(onvif_relay.pulse(CAMERA, "", "", 'out<&">1', 0)), 'out<&">1')
        self.assertEqual([content_type for content_type, _ in seen.seen], [f'application/soap+xml; charset=utf-8; action="{onvif_relay.SET_STATE}"'] * 2)
        self.assertEqual(seen.seen[0][1], (
            '<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">'
            f'<s:Header><s:Action s:mustUnderstand="1">{onvif_relay.SET_STATE}</s:Action></s:Header><s:Body>'
            "<tds:SetRelayOutputState><tds:RelayOutputToken>out&lt;&amp;&quot;&gt;1</tds:RelayOutputToken><tds:LogicalState>active</tds:LogicalState>"
            "</tds:SetRelayOutputState></s:Body></s:Envelope>"))
        self.assertIn("<tds:LogicalState>inactive</tds:LogicalState>", seen.seen[1][1])

    def test_a_refused_call_and_an_unsafe_empty_or_broken_answer_are_errors(self):
        with camera(onvif_relay, Recorder(soap("<fault/>", 500))), self.assertRaises(httpx.HTTPStatusError):
            run(onvif_relay.pulse(CAMERA, "", "", None, 0))
        for unsafe in UNSAFE:
            with camera(onvif_relay, Recorder(httpx.Response(200, content=unsafe))), self.assertRaisesRegex(ValueError, "ONVIF response XML is unsafe"):
                run(onvif_relay.pulse(CAMERA, "", "", None, 0))
        with camera(onvif_relay, Recorder(httpx.Response(200, content=b""))), self.assertRaisesRegex(ValueError, "ONVIF response size is invalid"):
            run(onvif_relay.pulse(CAMERA, "", "", None, 0))
        with camera(onvif_relay, Recorder(httpx.Response(200, content=b"<broken"))), self.assertRaises(ET.ParseError):
            run(onvif_relay.pulse(CAMERA, "", "", None, 0))

    def test_an_answer_is_read_up_to_the_limit_these_calls_share_and_refused_over_it(self):
        limit = onvif_stream.MAX_RESPONSE_BYTES
        self.assertEqual(limit, 1024 * 1024)
        # At the limit the answer is read (and lists no relay); one byte more and it is not read at all.
        with camera(onvif_relay, Recorder(httpx.Response(200, content=padded(b"<x/>", limit)))), self.assertRaises(onvif_relay.NoRelayOutputs):
            run(onvif_relay.pulse(CAMERA, "", "", None, 0))
        with camera(onvif_relay, Recorder(httpx.Response(200, content=padded(b"<x/>", limit + 1)))), self.assertRaisesRegex(ValueError, "ONVIF response size is invalid"):
            run(onvif_relay.pulse(CAMERA, "", "", None, 0))

    def test_the_first_relay_the_camera_lists_is_used_when_none_is_named(self):
        def answer(body: str) -> httpx.Response:
            return soap('<tds:GetRelayOutputsResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><tds:RelayOutputs token="relay-1"/></tds:GetRelayOutputsResponse>'
                        if "GetRelayOutputs" in body else "<ok/>")

        seen = Recorder(answer)
        with camera(onvif_relay, seen):
            self.assertEqual(run(onvif_relay.pulse(CAMERA, "", "", None, 0)), "relay-1")
        self.assertEqual(seen.seen[0][0], f'application/soap+xml; charset=utf-8; action="{onvif_relay.GET_RELAYS}"')
        self.assertEqual(len(seen.seen), 3)


class MaskCallTests(unittest.TestCase):
    def test_the_first_call_is_the_same_envelope_and_a_refusal_or_an_unsafe_answer_is_an_error(self):
        seen = Recorder(soap("<fault/>", 500))
        with camera(onvif_masks, seen), self.assertRaises(httpx.HTTPStatusError):
            run(onvif_masks.write_masks(CAMERA, "", "", [], []))
        action = "http://www.onvif.org/ver10/device/wsdl/GetServices"
        self.assertEqual(seen.seen, [(f'application/soap+xml; charset=utf-8; action="{action}"', (
            '<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tr2="http://www.onvif.org/ver20/media/wsdl" '
            f'xmlns:tt="http://www.onvif.org/ver10/schema"><s:Header><s:Action s:mustUnderstand="1">{action}</s:Action></s:Header>'
            "<s:Body><tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices></s:Body></s:Envelope>"))])
        with camera(onvif_masks, Recorder(httpx.Response(200, content=UNSAFE[2]))), self.assertRaisesRegex(ValueError, "ONVIF response XML is unsafe"):
            run(onvif_masks.write_masks(CAMERA, "", "", [], []))

    def test_tokens_from_the_camera_and_from_earlier_writes_are_escaped(self):
        def answer(body: str) -> httpx.Response:
            if "GetServices" in body:
                return soap(f'<tds:GetServicesResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><tds:Service><tds:Namespace>{onvif_masks.MEDIA2}</tds:Namespace>'
                            "<tds:XAddr>http://192.0.2.50/onvif/media2</tds:XAddr></tds:Service></tds:GetServicesResponse>")
            if "GetVideoSourceConfigurations" in body:
                return soap('<tr2:GetVideoSourceConfigurationsResponse xmlns:tr2="x"><tr2:Configurations token="src&lt;1&gt;"/></tr2:GetVideoSourceConfigurationsResponse>')
            if "CreateMask" in body:
                return soap('<tr2:CreateMaskResponse xmlns:tr2="x"><tr2:Token>mask-1</tr2:Token></tr2:CreateMaskResponse>')
            return soap("<ok/>")

        seen = Recorder(answer)
        with camera(onvif_masks, seen):
            self.assertEqual(run(onvif_masks.write_masks(CAMERA, "", "", [{"x": 0, "y": 0, "width": .5, "height": .5}], ['old"&'])), ["mask-1"])
        bodies = [body for _, body in seen.seen]
        self.assertIn("<tr2:GetMaskOptions><tr2:ConfigurationToken>src&lt;1&gt;</tr2:ConfigurationToken></tr2:GetMaskOptions>", bodies[2])
        self.assertIn("<tr2:DeleteMask><tr2:Token>old&quot;&amp;</tr2:Token></tr2:DeleteMask>", bodies[3])
        self.assertIn("<tt:ConfigurationToken>src&lt;1&gt;</tt:ConfigurationToken>", bodies[4])


class PtzCallTests(unittest.TestCase):
    def setUp(self):
        onvif_ptz._discovered.clear()
        self.addCleanup(onvif_ptz._discovered.clear)

    def client(self, handler, profile: str | None = 'main"<1>') -> onvif_ptz.PtzClient:
        ptz = onvif_ptz.PtzClient(CAMERA, "", "", profile)
        ptz._client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[method-assign]
        return ptz

    @staticmethod
    def answer(body: str) -> httpx.Response:
        if "GetCapabilities" in body:
            return soap('<tds:Capabilities xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><tt:PTZ xmlns:tt="x"><tt:XAddr>http://192.0.2.50/onvif/ptz</tt:XAddr></tt:PTZ></tds:Capabilities>')
        if "SetPreset" in body:
            return soap('<tptz:SetPresetResponse xmlns:tptz="x"><tptz:PresetToken>7</tptz:PresetToken></tptz:SetPresetResponse>')
        return soap("<ok/>")

    def test_the_first_call_is_the_same_envelope(self):
        seen = Recorder(self.answer)
        run(self.client(seen).stop())
        action = "http://www.onvif.org/ver10/device/wsdl/GetCapabilities"
        self.assertEqual(seen.seen[0], (f'application/soap+xml; charset=utf-8; action="{action}"', (
            '<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
            'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">'
            f'<s:Header><s:Action s:mustUnderstand="1">{action}</s:Action></s:Header>'
            "<s:Body><tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities></s:Body></s:Envelope>")))
        self.assertIn("<tptz:Stop><tptz:ProfileToken>main&quot;&lt;1&gt;</tptz:ProfileToken>", seen.seen[1][1])

    def test_a_preset_name_and_token_are_escaped_and_the_new_token_is_returned(self):
        seen = Recorder(self.answer)
        self.assertEqual(run(self.client(seen).set_preset('Gate & "dock"', "p<2>")), "7")
        self.assertIn('<tptz:PresetName>Gate &amp; &quot;dock&quot;</tptz:PresetName><tptz:PresetToken>p&lt;2&gt;</tptz:PresetToken>', seen.seen[-1][1])
        run(self.client(seen).goto_preset("p<2>"))
        self.assertIn("<tptz:PresetToken>p&lt;2&gt;</tptz:PresetToken></tptz:GotoPreset>", seen.seen[-1][1])

    def test_a_refused_call_and_an_unsafe_answer_are_errors(self):
        with self.assertRaises(httpx.HTTPStatusError):
            run(self.client(Recorder(soap("<fault/>", 500))).stop())
        with self.assertRaisesRegex(ValueError, "ONVIF response XML is unsafe"):
            run(self.client(Recorder(httpx.Response(200, content=UNSAFE[1]))).stop())


if __name__ == "__main__":
    unittest.main()
