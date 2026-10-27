"""Resolve camera RTSP stream URIs through the ONVIF Device and Media services.

The SOAP work is done by ``onvif_client`` (negotiated authentication, discovered services, Media1 and Media2); this module keeps the
entry points the camera APIs call, and the lookups that keep every request on the camera address the administrator entered."""
from __future__ import annotations

import ipaddress
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlsplit

from .discovery import IP_RANGE_PORTS
from .onvif_client import OnvifClient, OnvifError
from .onvif_profiles import MediaProfile, choose_profiles

SOAP = "http://www.w3.org/2003/05/soap-envelope"
DEVICE = "http://www.onvif.org/ver10/device/wsdl"
MEDIA = "http://www.onvif.org/ver10/media/wsdl"
SCHEMA = "http://www.onvif.org/ver10/schema"
MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CameraStreamSource:
    stream_url: str
    device_service_url: str


@dataclass(frozen=True)
class OnvifStreamProfile:
    token: str
    name: str
    encoding: str = "other"
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    source: str = "media1"
    suggested: str = ""  # "record" or "live" for the profile the NVR would choose, else empty


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _soap(action: str, body: str) -> bytes:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP}" xmlns:tds="{DEVICE}" xmlns:trt="{MEDIA}" xmlns:tt="{SCHEMA}">
 <s:Header><s:Action s:mustUnderstand="1">{action}</s:Action></s:Header>
 <s:Body>{body}</s:Body>
</s:Envelope>'''.encode("utf-8")


def _parse_response(data: bytes) -> ET.Element:
    if not data or len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("ONVIF response size is invalid")
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("ONVIF response XML is unsafe")
    return ET.fromstring(data)


def _camera_ip(value: str) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise ValueError("Enter a valid camera IPv4 address") from exc
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        raise ValueError("Enter a unicast camera IPv4 address")
    return address


def _stream_lookup_inputs(
    camera_ip_value: str,
    username: str,
    password: str,
    device_service_url: str | None,
    brand: str,
) -> tuple[ipaddress.IPv4Address, str, tuple[str, ...]]:
    address = _camera_ip(camera_ip_value)
    username = username.strip()
    if bool(username) != bool(password):
        raise ValueError("Enter both the camera username and password, or leave both blank")
    if device_service_url:
        parsed = urlsplit(device_service_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid ONVIF device service port") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Invalid ONVIF device service URL")
        if parsed.hostname != str(address):
            raise ValueError("ONVIF device service URL host must match the camera IP address")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Invalid ONVIF device service port")
        service_urls = (device_service_url,)
    else:
        service_urls = tuple(f"http://{address}:{port}/onvif/device_service" for port in _ports_for_brand(brand))
    return address, username, service_urls




def _ports_for_brand(brand: str) -> tuple[int, ...]:
    priorities = {
        "acti": (80, 8080),
        "axis": (80, 8080), "bosch": (80, 8080), "dahua": (80, 8080, 8000),
        "hanwha": (80, 8080), "hikvision": (80, 8000, 8080), "reolink": (80, 8000), "samsung": (80, 8080),
        "uniview": (80, 8080, 8000), "other": IP_RANGE_PORTS, "auto": IP_RANGE_PORTS,
    }
    preferred = priorities.get(brand.casefold(), IP_RANGE_PORTS)
    return tuple(dict.fromkeys(preferred + IP_RANGE_PORTS))


async def _services_and_profiles(client: OnvifClient, address: str, service_urls: tuple[str, ...], explicit: bool, brand: str):
    """Find the device service (the one given, or the first port that answers) and read its services and profiles."""
    if explicit:
        url = await client.locate_device(service_urls[0])
    else:
        url = await client.locate_device(None, _ports_for_brand(brand))
    services = await client.services(url)
    return url, services, await client.profiles(services)


async def resolve_camera_stream(
    camera_ip_value: str,
    username: str = "",
    password: str = "",
    device_service_url: str | None = None,
    profile_token: str | None = None,
    brand: str = "auto",
) -> CameraStreamSource:
    """Use ONVIF to resolve a stream URI without asking for a raw RTSP URL.

    With no ``profile_token`` the NVR chooses: the best H.264 profile (never MJPEG while H.264 is offered). A token that the camera does
    not have is an error, not a silent change to another profile."""
    address, username, service_urls = _stream_lookup_inputs(camera_ip_value, username, password, device_service_url, brand)
    try:
        async with OnvifClient(str(address), username, password) as client:
            url, services, profiles = await _services_and_profiles(client, str(address), service_urls, bool(device_service_url), brand)
            if profile_token:
                profile = next((item for item in profiles if item.token == profile_token), None)
                if profile is None:
                    raise ValueError("The selected ONVIF profile is not on the camera any more; choose a profile again")
            else:
                profile = choose_profiles(profiles).record or next((item for item in profiles if item.encoding == "other" and not item.width and not item.height), None)
                # A profile with no encoder details at all is tried anyway; the video check decides.
                if profile is None:
                    raise ValueError("No compatible video profile found on the camera")
            uri = await client.stream_uri(services, profile)
            return CameraStreamSource(client.stream_url_with_login(uri), url)
    except OnvifError as exc:
        raise ValueError(exc.message) from exc


async def list_camera_stream_profiles(
    camera_ip_value: str,
    username: str = "",
    password: str = "",
    device_service_url: str | None = None,
    brand: str = "auto",
) -> tuple[str, tuple[OnvifStreamProfile, ...]]:
    """Return all ONVIF media profiles (with codec, size and frame rate, and the ones the NVR would choose) without any credentialed URI."""
    address, username, service_urls = _stream_lookup_inputs(camera_ip_value, username, password, device_service_url, brand)
    try:
        async with OnvifClient(str(address), username, password) as client:
            url, _, profiles = await _services_and_profiles(client, str(address), service_urls, bool(device_service_url), brand)
    except OnvifError as exc:
        raise ValueError(exc.message) from exc
    if not profiles:
        raise ValueError("The camera listed no ONVIF media profiles")
    chosen = choose_profiles(profiles)
    marks = {chosen.record.token: "record"} if chosen.record else {}
    if chosen.live:
        marks[chosen.live.token] = "live"
    return url, tuple(
        OnvifStreamProfile(item.token, item.name, item.encoding, item.width, item.height, item.fps, item.has_audio, item.source, marks.get(item.token, ""))
        for item in profiles
    )
