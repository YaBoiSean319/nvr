"""What every reader and writer of a camera's SOAP shares: an element's name without its namespace, text escaped for a request, the refusal of
a document that declares a DOCTYPE or an entity, and one call that sends an envelope and reads the answer.

These were written out once per module (the guard in five places). A change to how a camera's XML is read is now made here; each caller keeps
its own size limit and its own way of reporting a refusal (see tests/test_onvif_soap.py)."""
from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx


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
    return b"<!DOCTYPE" in upper or b"<!ENTITY" in upper


def parse_bounded(data: bytes, limit: int) -> ET.Element:
    """A camera's answer as a parsed document. ``ValueError`` when it is empty, over ``limit`` bytes or unsafe; ``ET.ParseError`` when it is not XML."""
    if not data or len(data) > limit:
        raise ValueError("ONVIF response size is invalid")
    if declares_dtd(data):
        raise ValueError("ONVIF response XML is unsafe")
    return ET.fromstring(data)


async def call(client: httpx.AsyncClient, url: str, action: str, envelope: bytes, limit: int) -> ET.Element:
    """Send one SOAP request and return the parsed answer. An HTTP error status raises ``httpx.HTTPStatusError``; the answer is read as ``parse_bounded`` reads it."""
    response = await client.post(url, content=envelope, headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'})
    response.raise_for_status()
    return parse_bounded(response.content, limit)
