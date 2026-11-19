import unittest

from app.discovery import IP_RANGE_MAX_HOSTS, parse_camera_ip_range
from app.onvif_stream import _stream_lookup_inputs


class CameraRangeTests(unittest.TestCase):
    def test_accepts_private_ranges_with_bounded_host_count(self):
        hosts = parse_camera_ip_range("10.45.50.0/24")
        self.assertEqual(len(hosts), 254)
        self.assertEqual(str(hosts[0]), "10.45.50.1")
        self.assertEqual(str(hosts[-1]), "10.45.50.254")
        self.assertEqual(len(parse_camera_ip_range("192.168.1.7/32")), 1)

    def test_rejects_public_oversized_and_ipv6_ranges(self):
        for value in ("8.8.8.0/24", "10.0.0.0/16", "2001:db8::/120", "not-a-range"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_camera_ip_range(value)
        self.assertGreater(IP_RANGE_MAX_HOSTS, 0)

    def test_custom_onvif_path_must_stay_on_entered_camera(self):
        address, username, urls = _stream_lookup_inputs(
            "10.45.50.25", "operator", "secret", "http://10.45.50.25:8080/custom/onvif", "hikvision"
        )
        self.assertEqual(str(address), "10.45.50.25")
        self.assertEqual(username, "operator")
        self.assertEqual(urls, ("http://10.45.50.25:8080/custom/onvif",))
        with self.assertRaisesRegex(ValueError, "host must match"):
            _stream_lookup_inputs("10.45.50.25", "", "", "http://example.test/onvif/device_service", "auto")


if __name__ == "__main__":
    unittest.main()


class RangeProbeAnswerTests(unittest.TestCase):
    """The address-range scan reads a device's whole answer, and a fault counts only when it is an ONVIF one (#381)."""

    ONVIF = (b'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
             b"<s:Body><tds:GetDeviceInformationResponse><tds:Model>X</tds:Model></tds:GetDeviceInformationResponse></s:Body></s:Envelope>")
    ONVIF_FAULT = (b'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:ter="http://www.onvif.org/ver10/error">'
                   b"<s:Body><s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>ter:NotAuthorized</s:Value></s:Subcode></s:Code></s:Fault></s:Body></s:Envelope>")
    OTHER_FAULT = (b'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                   b"<s:Body><s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code><s:Reason><s:Text>Unknown action</s:Text></s:Reason></s:Fault></s:Body></s:Envelope>")

    def probe(self, body: bytes, *, in_pieces: bool = False, keep_open: bool = False):
        import asyncio
        import ipaddress

        from app import discovery

        async def run():
            async def answer(reader, writer):
                await reader.readuntil(b"</s:Envelope>")
                head = b"HTTP/1.1 200 OK\r\nContent-Type: application/soap+xml\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n"
                if in_pieces:
                    writer.write(head)
                    await writer.drain()
                    await asyncio.sleep(0.15)  # The headers arrive first; the body follows a moment later
                    writer.write(body[: len(body) // 2])
                    await writer.drain()
                    await asyncio.sleep(0.15)
                    writer.write(body[len(body) // 2:])
                else:
                    writer.write(head + body)
                await writer.drain()
                if keep_open:
                    await asyncio.sleep(5)  # A device that does not close the connection when asked
                writer.close()

            server = await asyncio.start_server(answer, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                return await discovery._probe_onvif_endpoint(ipaddress.IPv4Address("127.0.0.1"), port, asyncio.Semaphore(1))
            finally:
                server.close()

        return asyncio.run(run())

    def test_a_device_whose_answer_arrives_in_pieces_is_recognised(self):
        self.assertIsNotNone(self.probe(self.ONVIF))
        self.assertIsNotNone(self.probe(self.ONVIF, in_pieces=True), "only the first piece of the answer was read")

    def test_a_device_that_keeps_the_connection_open_is_recognised_once_its_answer_is_complete(self):
        self.assertIsNotNone(self.probe(self.ONVIF, keep_open=True))

    def test_an_onvif_fault_is_a_camera_and_another_services_fault_is_not(self):
        self.assertIsNotNone(self.probe(self.ONVIF_FAULT))
        self.assertIsNone(self.probe(self.OTHER_FAULT), "a SOAP service that is not a camera was listed as one")
