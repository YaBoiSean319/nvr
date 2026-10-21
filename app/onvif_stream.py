"""Resolve camera RTSP stream URIs through the ONVIF Device and Media services."""
from __future__ import annotations

import ipaddress
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .discovery import IP_RANGE_PORTS

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


def _with_credentials(uri: str, camera_ip: ipaddress.IPv4Address, username: str, password: str) -> str:
    parsed = urlsplit(uri.strip())
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise ValueError("Camera did not return a valid RTSP stream URI")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Camera returned an invalid RTSP port") from exc
    # Use the address entered by the administrator, not a hostname or embedded
    # credentials supplied in the camera's SOAP response.
    host = str(camera_ip)
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{host}" if username else host
    if port is not None:
        netloc += f":{port}"
    if any(char.isspace() or char in "'\\\x00" for char in parsed.path + parsed.query):
        raise ValueError("Camera returned an invalid RTSP URI")
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


async def _resolve_at_service(
    client: httpx.AsyncClient,
    camera_ip: ipaddress.IPv4Address,
    service_url: str,
    username: str,
    password: str,
    profile_token: str | None = None,
) -> CameraStreamSource:
    device_action = f"{DEVICE}/GetCapabilities"
    capabilities_response = await client.post(
        service_url,
        content=_soap(device_action, "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>"),
        headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{device_action}"'},
    )
    capabilities_response.raise_for_status()
    capabilities = _parse_response(capabilities_response.content)
    media_url = None
    for node in capabilities.iter():
        if _local(node.tag) == "Media":
            media_url = next((child.text.strip() for child in node.iter() if _local(child.tag) == "XAddr" and child.text), None)
            if media_url:
                break
    if not media_url:
        raise ValueError("Camera did not report an ONVIF media service")
    media = urlsplit(media_url)
    if media.scheme not in {"http", "https"} or not media.hostname or media.username or media.password:
        raise ValueError("Camera returned an invalid ONVIF media service URL")
    # Keep media requests on the camera address that the administrator supplied.
    media_netloc = str(camera_ip) + (f":{media.port}" if media.port else "")
    media_url = urlunsplit((media.scheme, media_netloc, media.path, "", ""))

    profiles_action = f"{MEDIA}/GetProfiles"
    profiles_response = await client.post(
        media_url,
        content=_soap(profiles_action, "<trt:GetProfiles/>"),
        headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{profiles_action}"'},
    )
    profiles_response.raise_for_status()
    profiles = _parse_response(profiles_response.content)
    available_profiles = [node for node in profiles.iter() if _local(node.tag) == "Profiles" and node.attrib.get("token")]
    profile = next((node for node in available_profiles if node.attrib.get("token") == profile_token), None) if profile_token else None
    profile = profile or (available_profiles[0] if available_profiles else None)
    if profile is None:
        raise ValueError("Camera did not return an ONVIF media profile")
    token = profile.attrib["token"]
    escaped_token = (token.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))
    stream_action = f"{MEDIA}/GetStreamUri"
    body = (
        "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
        "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
        f"<trt:ProfileToken>{escaped_token}</trt:ProfileToken></trt:GetStreamUri>"
    )
    stream_response = await client.post(
        media_url,
        content=_soap(stream_action, body),
        headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{stream_action}"'},
    )
    stream_response.raise_for_status()
    stream_doc = _parse_response(stream_response.content)
    uri = next((node.text.strip() for node in stream_doc.iter() if _local(node.tag) == "Uri" and node.text), None)
    if not uri:
        raise ValueError("Camera did not return an RTSP stream URI")
    return CameraStreamSource(_with_credentials(uri, camera_ip, username, password), service_url)


async def resolve_camera_stream(
    camera_ip_value: str,
    username: str = "",
    password: str = "",
    device_service_url: str | None = None,
    profile_token: str | None = None,
    brand: str = "auto",
) -> CameraStreamSource:
    """Use ONVIF to resolve a stream URI without asking for a raw RTSP URL."""
    address, username, service_urls = _stream_lookup_inputs(camera_ip_value, username, password, device_service_url, brand)

    auth = httpx.DigestAuth(username, password) if username else None
    async with httpx.AsyncClient(auth=auth, timeout=httpx.Timeout(3.0, connect=1.5), trust_env=False, follow_redirects=False) as client:
        for service_url in service_urls:
            try:
                return await _resolve_at_service(client, address, service_url, username, password, profile_token)
            except (httpx.HTTPError, OSError, ValueError, ET.ParseError):
                continue
    raise ValueError("Could not retrieve a stream URI over ONVIF; check the camera IP, credentials, and ONVIF service")


def _ports_for_brand(brand: str) -> tuple[int, ...]:
    priorities = {
        "acti": (80, 8080),
        "axis": (80, 8080), "bosch": (80, 8080), "dahua": (80, 8080, 8000),
        "hanwha": (80, 8080), "hikvision": (80, 8000, 8080), "reolink": (80, 8000), "samsung": (80, 8080),
        "uniview": (80, 8080, 8000), "other": IP_RANGE_PORTS, "auto": IP_RANGE_PORTS,
    }
    preferred = priorities.get(brand.casefold(), IP_RANGE_PORTS)
    return tuple(dict.fromkeys(preferred + IP_RANGE_PORTS))


async def list_camera_stream_profiles(
    camera_ip_value: str,
    username: str = "",
    password: str = "",
    device_service_url: str | None = None,
    brand: str = "auto",
) -> tuple[str, tuple[OnvifStreamProfile, ...]]:
    """Return all ONVIF media profiles without exposing any credentialed URI."""
    address, username, service_urls = _stream_lookup_inputs(camera_ip_value, username, password, device_service_url, brand)
    auth = httpx.DigestAuth(username, password) if username else None
    async with httpx.AsyncClient(auth=auth, timeout=httpx.Timeout(3.0, connect=1.5), trust_env=False, follow_redirects=False) as client:
        for service_url in service_urls:
            try:
                action = f"{DEVICE}/GetCapabilities"
                response = await client.post(
                    service_url,
                    content=_soap(action, "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>"),
                    headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'},
                )
                response.raise_for_status()
                root = _parse_response(response.content)
                media_url = next((n.text.strip() for parent in root.iter() if _local(parent.tag) == "Media"
                                  for n in parent.iter() if _local(n.tag) == "XAddr" and n.text), None)
                if not media_url:
                    continue
                parsed_media = urlsplit(media_url)
                if parsed_media.scheme not in {"http", "https"} or not parsed_media.hostname or parsed_media.username or parsed_media.password:
                    continue
                netloc = str(address) + (f":{parsed_media.port}" if parsed_media.port else "")
                media_url = urlunsplit((parsed_media.scheme, netloc, parsed_media.path, "", ""))
                action = f"{MEDIA}/GetProfiles"
                profiles_response = await client.post(
                    media_url, content=_soap(action, "<trt:GetProfiles/>"),
                    headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'},
                )
                profiles_response.raise_for_status()
                profiles_root = _parse_response(profiles_response.content)
                profiles = tuple(
                    OnvifStreamProfile(node.attrib["token"], next((child.text.strip() for child in node.iter() if _local(child.tag) == "Name" and child.text), node.attrib["token"])[:128])
                    for node in profiles_root.iter() if _local(node.tag) == "Profiles" and node.attrib.get("token")
                )
                if profiles:
                    return service_url, profiles
            except (httpx.HTTPError, OSError, ValueError, ET.ParseError):
                continue
    raise ValueError("Could not retrieve ONVIF stream profiles; check camera IP, brand, credentials, and ONVIF service")
