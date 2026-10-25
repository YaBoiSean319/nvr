"""Write privacy masks to a camera through ONVIF Media2, so its recordings carry them.

ONVIF masks are polygons in the video source's normalized space (-1..1, y up). Each
rectangle drawn in the NVR becomes one filled mask on the camera's video source
configuration. Masks the NVR created before are deleted first, by their tokens.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from .onvif_stream import DEVICE, SCHEMA, SOAP, _local, _parse_response

MEDIA2 = "http://www.onvif.org/ver20/media/wsdl"


class MasksUnsupported(Exception):
    """The camera has no Media2 service or does not accept privacy masks."""


class MaskWriteFailed(Exception):
    """Writing stopped part way; ``created`` names the masks already on the camera, so they can be removed later."""

    def __init__(self, created: list[str]) -> None:
        super().__init__("Privacy masks were only partly written to the camera")
        self.created = created


def _envelope(action: str, body: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{SOAP}" xmlns:tds="{DEVICE}" xmlns:tr2="{MEDIA2}" '
            f'xmlns:tt="{SCHEMA}"><s:Header><s:Action s:mustUnderstand="1">{action}</s:Action></s:Header>'
            f"<s:Body>{body}</s:Body></s:Envelope>").encode("utf-8")


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def polygon(zone: dict[str, float]) -> list[tuple[float, float]]:
    """A rectangle from 0..1 picture space (top left origin) as ONVIF points (-1..1, y up)."""
    left, top = zone["x"] * 2 - 1, 1 - zone["y"] * 2
    right, bottom = (zone["x"] + zone["width"]) * 2 - 1, 1 - (zone["y"] + zone["height"]) * 2
    return [(left, top), (right, top), (right, bottom), (left, bottom)]


async def _call(client: httpx.AsyncClient, url: str, action: str, body: str) -> ET.Element:
    response = await client.post(url, content=_envelope(action, body), headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'})
    response.raise_for_status()
    return _parse_response(response.content)


async def write_masks(device_service_url: str, username: str, password: str, zones: list[dict[str, float]], previous_tokens: list[str]) -> list[str]:
    """Replace the NVR's masks on the camera with ``zones``. Returns the new mask tokens."""
    auth = httpx.DigestAuth(username, password) if username else None
    async with httpx.AsyncClient(auth=auth, timeout=httpx.Timeout(10, connect=5), trust_env=False, follow_redirects=False) as client:
        services = await _call(client, device_service_url, f"{DEVICE}/GetServices", "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>")
        media2 = None
        for service in services.iter():
            if _local(service.tag) != "Service":
                continue
            namespace = next((child.text for child in service if _local(child.tag) == "Namespace"), None)
            if namespace == MEDIA2:
                media2 = next((child.text.strip() for child in service if _local(child.tag) == "XAddr" and child.text), None)
        if not media2:
            raise MasksUnsupported()
        configurations = await _call(client, media2, f"{MEDIA2}/GetVideoSourceConfigurations", "<tr2:GetVideoSourceConfigurations/>")
        # Every video source: a stream from a source left unmasked would carry the private area.
        sources = list(dict.fromkeys(node.get("token") for node in configurations.iter() if _local(node.tag) == "Configurations" and node.get("token")))
        if not sources:
            raise MasksUnsupported()
        for source in sources:
            try:
                await _call(client, media2, f"{MEDIA2}/GetMaskOptions", f"<tr2:GetMaskOptions><tr2:ConfigurationToken>{_escape(source)}</tr2:ConfigurationToken></tr2:GetMaskOptions>")
            except httpx.HTTPStatusError as exc:
                raise MasksUnsupported() from exc
        for token in previous_tokens:
            try:
                await _call(client, media2, f"{MEDIA2}/DeleteMask", f"<tr2:DeleteMask><tr2:Token>{_escape(token)}</tr2:Token></tr2:DeleteMask>")
            except httpx.HTTPStatusError:
                pass  # Already gone on the camera.
        tokens: list[str] = []
        try:
            for source in sources:
                await _create(client, media2, source, zones, tokens)
            return tokens
        except MasksUnsupported:
            raise
        except Exception as exc:
            raise MaskWriteFailed(tokens) from exc


async def _create(client: httpx.AsyncClient, media2: str, source: str, zones: list[dict[str, float]], tokens: list[str]) -> None:
    """Create each mask on one video source, adding its token to ``tokens`` as soon as the camera has it."""
    for zone in zones:
        points = "".join(f'<tt:Point x="{x:.4f}" y="{y:.4f}"/>' for x, y in polygon(zone))
        reply = await _call(client, media2, f"{MEDIA2}/CreateMask",
                            f"<tr2:CreateMask><tr2:Mask><tt:ConfigurationToken>{_escape(source)}</tt:ConfigurationToken>"
                            f"<tt:Polygon>{points}</tt:Polygon><tt:Type>Color</tt:Type>"
                            '<tt:Color X="0" Y="0" Z="0" Colorspace="http://www.onvif.org/ver10/colorspace/YCbCr"/>'
                            "<tt:Enabled>true</tt:Enabled></tr2:Mask></tr2:CreateMask>")
        token = next((node.text.strip() for node in reply.iter() if _local(node.tag) == "Token" and node.text), None)
        if not token:
            raise ValueError("The camera did not return a mask token")
        tokens.append(token)
