"""What every reader and writer of a camera's SOAP shares: an element's name without its namespace, text escaped for a request, the refusal of
a document that declares a DOCTYPE or an entity, and one call that sends an envelope and reads the answer.

These were written out once per module (the guard in five places). A change to how a camera's XML is read is now made here; each caller keeps
its own size limit and its own way of reporting a refusal (see tests/test_onvif_soap.py)."""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
import zlib
from xml.parsers import expat

import httpx

CALL_DEADLINE_SECONDS = 30.0  # For a whole PTZ, privacy-mask or relay call: the HTTP timeout is per read, and a camera can answer a byte at a time


def local(tag: str) -> str:
    """An element's name without its namespace."""
    return tag.rsplit("}", 1)[-1]


def escape(value: str) -> str:
    """Text made safe to place inside an element or a quoted attribute of a request."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def declares_dtd(data: bytes) -> bool:
    """Whether a document declares a DOCTYPE or an entity, in any letter case. Nothing a camera sends needs one, and they are how XML is made to
    expand without limit or to read other files, so such a document is never parsed."""
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        return True
    # The words are not there as plain bytes in a document sent as UTF-16 or UTF-32, which the parser reads all the same (#378). Such a document
    # starts with a byte-order mark or has zero bytes among its first four: ask the parser itself whether it declares anything.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in data[:4]:
        return _parser_sees_declaration(data)
    return False


class _Declared(Exception):
    pass


def _parser_sees_declaration(data: bytes) -> bool:
    def declared(*_: object) -> None:
        raise _Declared

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = declared
    parser.EntityDeclHandler = declared
    try:
        parser.Parse(data, True)
    except _Declared:
        return True
    except expat.ExpatError:
        return False  # Not XML at all: the caller's own parse says so
    return False


async def read_capped(response: httpx.Response, limit: int) -> bytes:
    """A streamed answer's body, read no further than ``limit`` bytes: ``ValueError`` once it is longer.

    The limit used to be checked after the HTTP client had downloaded and decompressed the whole answer, so it limited nothing: a camera, or
    anything answering at its address, could send a few hundred kilobytes that unpack to gigabytes and have the recorder killed for lack of
    memory (#378). The request asks for an uncompressed answer; one that is compressed anyway is unpacked here, never past the limit."""
    if response.is_stream_consumed:  # Already in memory (a transport that does not stream): there is nothing left to bound but its size
        if len(response.content) > limit:
            raise ValueError("ONVIF response size is invalid")
        return response.content
    encoding = response.headers.get("content-encoding", "identity").strip().casefold()
    if encoding not in {"", "identity", "gzip", "x-gzip", "deflate"}:
        raise ValueError("ONVIF response size is invalid")  # An encoding that was not asked for and cannot be unpacked within a bound
    received = bytearray()
    async for chunk in response.aiter_raw():
        received.extend(chunk)
        if len(received) > limit:
            raise ValueError("ONVIF response size is invalid")
    if encoding in {"", "identity"}:
        return bytes(received)
    try:
        unpacked = zlib.decompressobj(wbits=47).decompress(bytes(received), limit + 1)  # 47: gzip or zlib, whichever it is
    except zlib.error:
        raise ValueError("ONVIF response size is invalid") from None
    if len(unpacked) > limit:
        raise ValueError("ONVIF response size is invalid")
    return unpacked


def parse_bounded(data: bytes, limit: int) -> ET.Element:
    """A camera's answer as a parsed document. ``ValueError`` when it is empty, over ``limit`` bytes or unsafe; ``ET.ParseError`` when it is not XML."""
    if not data or len(data) > limit:
        raise ValueError("ONVIF response size is invalid")
    if declares_dtd(data):
        raise ValueError("ONVIF response XML is unsafe")
    return ET.fromstring(data)


async def call(client: httpx.AsyncClient, url: str, action: str, envelope: bytes, limit: int) -> ET.Element:
    """Send one SOAP request and return the parsed answer. An HTTP error status raises ``httpx.HTTPStatusError``; the answer is read no further
    than ``limit`` and then as ``parse_bounded`` reads it; a call that has not finished within ``CALL_DEADLINE_SECONDS`` raises ``httpx.ReadTimeout``."""
    headers = {"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"', "Accept-Encoding": "identity"}
    try:
        async with asyncio.timeout(CALL_DEADLINE_SECONDS):
            async with client.stream("POST", url, content=envelope, headers=headers) as response:
                response.raise_for_status()
                data = await read_capped(response, limit)
    except TimeoutError:
        raise httpx.ReadTimeout("The camera did not finish answering in time") from None
    return parse_bounded(data, limit)
