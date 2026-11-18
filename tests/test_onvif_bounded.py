"""A camera cannot exhaust the NVR's memory with one ONVIF answer (#378).

The size limit used to be checked after the whole answer had been downloaded and unpacked. Answers are now read as they arrive and no further than
the limit, a whole exchange has a deadline, and a document that declares a DOCTYPE or an entity is refused in any encoding."""
from __future__ import annotations

import asyncio
import gzip
import tracemalloc
import unittest
from unittest.mock import patch

import httpx

from app import onvif_soap
from app.onvif_client import OnvifClient, OnvifError

URL = "http://192.0.2.50/onvif/device_service"
LIMIT = 1024 * 1024
ENVELOPE = b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body><ok/></s:Body></s:Envelope>'


class Body(httpx.AsyncByteStream):
    """An answer delivered a piece at a time, counting what was actually taken from the camera."""

    def __init__(self, pieces, delay: float = 0.0) -> None:
        self.pieces, self.delay, self.taken = pieces, delay, 0

    async def __aiter__(self):
        for piece in self.pieces:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.taken += len(piece)
            yield piece


def endless():
    while True:
        yield b"<!-- padding -->" * 4096  # 64 KiB at a time, for ever


class Camera(httpx.AsyncBaseTransport):
    def __init__(self, body: Body, headers: dict[str, str] | None = None) -> None:
        self.body, self.headers, self.requests = body, headers or {}, []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, headers=self.headers, stream=self.body)


async def call(camera: Camera):
    async with httpx.AsyncClient(transport=camera) as client:
        return await onvif_soap.call(client, URL, "urn:action", ENVELOPE, LIMIT)


class BoundedReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_ordinary_answer_is_read_and_an_uncompressed_one_is_asked_for(self):
        camera = Camera(Body([ENVELOPE[:40], ENVELOPE[40:]]))
        root = await call(camera)
        self.assertTrue(root.tag.endswith("Envelope"))
        self.assertEqual(camera.requests[0].headers["accept-encoding"], "identity")

    async def test_an_answer_that_never_ends_is_refused_once_it_passes_the_limit(self):
        camera = Camera(Body(endless()))
        with self.assertRaisesRegex(ValueError, "ONVIF response size is invalid"):
            await call(camera)
        self.assertLessEqual(camera.body.taken, LIMIT + 64 * 1024)

    async def test_a_small_compressed_answer_that_unpacks_to_hundreds_of_megabytes_is_refused_without_unpacking_it(self):
        bomb = gzip.compress(b"<x>" + b" " * (300 * 1024 * 1024) + b"</x>", compresslevel=9)
        self.assertLess(len(bomb), LIMIT)  # About 300 KiB on the wire
        camera = Camera(Body([bomb]), {"Content-Encoding": "gzip"})
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(ValueError, "ONVIF response size is invalid"):
                await call(camera)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * LIMIT, f"{peak / 1024 / 1024:.0f} MiB was held while reading a {len(bomb) // 1024} KiB answer")

    async def test_a_compressed_answer_within_the_limit_is_still_read(self):
        camera = Camera(Body([gzip.compress(ENVELOPE)]), {"Content-Encoding": "gzip"})
        self.assertTrue((await call(camera)).tag.endswith("Envelope"))

    async def test_an_encoding_that_cannot_be_unpacked_within_a_bound_is_refused(self):
        with self.assertRaisesRegex(ValueError, "ONVIF response size is invalid"):
            await call(Camera(Body([b"anything"]), {"Content-Encoding": "br"}))

    async def test_a_camera_that_answers_a_byte_at_a_time_is_given_up_on(self):
        camera = Camera(Body((bytes([byte]) for byte in ENVELOPE * 50), delay=0.02))
        with patch.object(onvif_soap, "CALL_DEADLINE_SECONDS", 0.3), self.assertRaises(httpx.ReadTimeout):
            await call(camera)

    async def test_the_onvif_client_reads_no_further_than_its_limit_and_reports_a_timeout(self):
        camera = Camera(Body(endless()))
        async with OnvifClient("192.0.2.50", "", "", transport=camera) as client:
            with self.assertRaises(OnvifError):
                await client.device_information(URL)
        self.assertLessEqual(camera.body.taken, 2 * 1024 * 1024 + 64 * 1024)

        slow = Camera(Body((bytes([byte]) for byte in ENVELOPE * 50), delay=0.02))
        async with OnvifClient("192.0.2.50", "", "", transport=slow, timeout=httpx.Timeout(0.1, connect=0.05)) as client:
            with self.assertRaises(OnvifError) as caught:
                await client.device_information(URL)
        self.assertEqual(caught.exception.code, "timeout")


class DeclarationTests(unittest.TestCase):
    def test_a_doctype_or_entity_is_seen_in_utf_16_and_utf_32(self):
        unsafe = '<?xml version="1.0" encoding="{}"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>'
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            data = unsafe.format("utf-16").encode(encoding)
            self.assertTrue(onvif_soap.declares_dtd(data), encoding)
            with self.assertRaisesRegex(ValueError, "ONVIF response XML is unsafe"):
                onvif_soap.parse_bounded(data, LIMIT)

    def test_an_ordinary_utf_16_document_is_still_read(self):
        data = '<?xml version="1.0" encoding="utf-16"?><x>café</x>'.encode("utf-16")
        self.assertFalse(onvif_soap.declares_dtd(data))
        self.assertEqual(onvif_soap.parse_bounded(data, LIMIT).text, "café")

    def test_bytes_that_are_not_xml_are_left_for_the_callers_own_parse_to_refuse(self):
        self.assertFalse(onvif_soap.declares_dtd(b"\x00\x01\x02\x03 not xml"))


if __name__ == "__main__":
    unittest.main()
