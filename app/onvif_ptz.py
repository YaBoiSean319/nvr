"""ONVIF PTZ: continuous moves, stop, centring on a point, and presets.

The NVR talks to the camera; browsers never do. The PTZ service address comes from
the device's capabilities, and the media profile from the camera's saved profile
token or, failing that, its first profile.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from .onvif_stream import DEVICE, MEDIA, SCHEMA, SOAP, _local, _parse_response

PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
FOV_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov"


class PtzUnsupported(Exception):
    """The camera has no PTZ service, or none for its media profile."""


@dataclass
class Capabilities:
    pan_tilt: bool = False
    zoom: bool = False
    centre: bool = False  # Relative moves in field-of-view space: click-to-centre.
    max_presets: int = 0
    spaces: list[str] = field(default_factory=list)


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _number(value: float) -> str:
    return f"{max(-1.0, min(1.0, float(value))):.4f}"


def _envelope(action: str, body: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{SOAP}" xmlns:tds="{DEVICE}" xmlns:trt="{MEDIA}" '
            f'xmlns:tptz="{PTZ}" xmlns:tt="{SCHEMA}"><s:Header><s:Action s:mustUnderstand="1">{action}</s:Action></s:Header>'
            f"<s:Body>{body}</s:Body></s:Envelope>").encode("utf-8")


# Where each device's PTZ service and media profile are, once discovered: (device URL, saved profile) → (PTZ URL, profile).
_discovered: dict[tuple[str, str | None], tuple[str, str]] = {}


class PtzClient:
    def __init__(self, device_service_url: str, username: str, password: str, profile_token: str | None) -> None:
        self.device_service_url = device_service_url
        self.auth = httpx.DigestAuth(username, password) if username else None
        self.profile_token = profile_token
        self.ptz_url: str | None = None

    async def _call(self, client: httpx.AsyncClient, url: str, action: str, body: str) -> ET.Element:
        response = await client.post(url, content=_envelope(action, body), headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'})
        response.raise_for_status()
        return _parse_response(response.content)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(auth=self.auth, timeout=httpx.Timeout(8, connect=4), trust_env=False, follow_redirects=False)

    async def _prepare(self, client: httpx.AsyncClient) -> str:
        if self.ptz_url and self.profile_token:
            return self.ptz_url
        known = _discovered.get((self.device_service_url, self.profile_token))
        if known:
            self.ptz_url, self.profile_token = known
            return self.ptz_url
        saved_profile = self.profile_token
        capabilities = await self._call(client, self.device_service_url, f"{DEVICE}/GetCapabilities",
                                        "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>")
        addresses: dict[str, str] = {}
        for node in capabilities.iter():
            if _local(node.tag) in {"PTZ", "Media"}:
                xaddr = next((child.text.strip() for child in node if _local(child.tag) == "XAddr" and child.text), None)
                if xaddr:
                    addresses[_local(node.tag)] = xaddr
        if "PTZ" not in addresses:
            raise PtzUnsupported()
        self.ptz_url = addresses["PTZ"]
        if not self.profile_token:
            profiles = await self._call(client, addresses.get("Media", self.device_service_url), f"{MEDIA}/GetProfiles", "<trt:GetProfiles/>")
            self.profile_token = next((node.get("token") for node in profiles.iter() if _local(node.tag) == "Profiles" and node.get("token")), None)
            if not self.profile_token:
                raise PtzUnsupported()
        _discovered[(self.device_service_url, saved_profile)] = (self.ptz_url, self.profile_token)
        return self.ptz_url

    def _profile(self) -> str:
        return f"<tptz:ProfileToken>{_escape(self.profile_token or '')}</tptz:ProfileToken>"

    async def capabilities(self) -> Capabilities:
        async with self._client() as client:
            url = await self._prepare(client)
            nodes = await self._call(client, url, f"{PTZ}/GetNodes", "<tptz:GetNodes/>")
        found = Capabilities()
        for node in nodes.iter():
            name = _local(node.tag)
            if name in {"ContinuousPanTiltVelocitySpace", "AbsolutePanTiltPositionSpace", "RelativePanTiltTranslationSpace"}:
                found.pan_tilt = True
            if name in {"ContinuousZoomVelocitySpace", "AbsoluteZoomPositionSpace", "RelativeZoomTranslationSpace"}:
                found.zoom = True
            if name == "RelativePanTiltTranslationSpace":
                uri = next((child.text.strip() for child in node if _local(child.tag) == "URI" and child.text), "")
                found.spaces.append(uri)
                found.centre = found.centre or uri == FOV_SPACE
            if name == "MaximumNumberOfPresets" and node.text and node.text.strip().isdigit():
                found.max_presets = int(node.text.strip())
        return found

    async def continuous_move(self, pan: float, tilt: float, zoom: float, timeout_seconds: float) -> None:
        velocity = ""
        if pan or tilt:
            velocity += f'<tt:PanTilt x="{_number(pan)}" y="{_number(tilt)}"/>'
        if zoom:
            velocity += f'<tt:Zoom x="{_number(zoom)}"/>'
        async with self._client() as client:
            url = await self._prepare(client)
            await self._call(client, url, f"{PTZ}/ContinuousMove",
                             f"<tptz:ContinuousMove>{self._profile()}<tptz:Velocity>{velocity}</tptz:Velocity>"
                             f"<tptz:Timeout>PT{max(0.1, min(timeout_seconds, 10)):.1f}S</tptz:Timeout></tptz:ContinuousMove>")

    async def stop(self) -> None:
        async with self._client() as client:
            url = await self._prepare(client)
            await self._call(client, url, f"{PTZ}/Stop", f"<tptz:Stop>{self._profile()}<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>")

    async def centre(self, x: float, y: float) -> None:
        """Bring the point (x, y) of the picture, 0..1 from the top left, to the centre."""
        async with self._client() as client:
            url = await self._prepare(client)
            await self._call(client, url, f"{PTZ}/RelativeMove",
                             f"<tptz:RelativeMove>{self._profile()}<tptz:Translation>"
                             f'<tt:PanTilt x="{_number((x - 0.5) * 2)}" y="{_number((0.5 - y) * 2)}" space="{FOV_SPACE}"/>'
                             "</tptz:Translation></tptz:RelativeMove>")

    async def set_preset(self, name: str, token: str | None = None) -> str:
        token_part = f"<tptz:PresetToken>{_escape(token)}</tptz:PresetToken>" if token else ""
        async with self._client() as client:
            url = await self._prepare(client)
            reply = await self._call(client, url, f"{PTZ}/SetPreset",
                                     f"<tptz:SetPreset>{self._profile()}<tptz:PresetName>{_escape(name)}</tptz:PresetName>{token_part}</tptz:SetPreset>")
        created = next((node.text.strip() for node in reply.iter() if _local(node.tag) == "PresetToken" and node.text), None)
        if not created:
            raise ValueError("The camera did not return a preset token")
        return created

    async def goto_preset(self, token: str) -> None:
        async with self._client() as client:
            url = await self._prepare(client)
            await self._call(client, url, f"{PTZ}/GotoPreset", f"<tptz:GotoPreset>{self._profile()}<tptz:PresetToken>{_escape(token)}</tptz:PresetToken></tptz:GotoPreset>")

    async def remove_preset(self, token: str) -> None:
        async with self._client() as client:
            url = await self._prepare(client)
            await self._call(client, url, f"{PTZ}/RemovePreset", f"<tptz:RemovePreset>{self._profile()}<tptz:PresetToken>{_escape(token)}</tptz:PresetToken></tptz:RemovePreset>")
