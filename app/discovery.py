from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


MULTICAST_ADDRESS = "239.255.255.250"
MULTICAST_PORT = 3702
MAX_DATAGRAM_BYTES = 64 * 1024
DISCOVERY_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class DiscoveredDevice:
    device_uuid: str
    service_urls: tuple[str, ...]
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryResult:
    devices: tuple[DiscoveredDevice, ...]
    ignored_responses: int


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _safe_service_url(value: str) -> str | None:
    """Keep only an HTTP(S) service origin/path; discovery URLs can carry secrets."""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        netloc = host if parsed.port is None else f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path[:512], "", ""))
    except ValueError:
        return None


def _safe_scope(value: str) -> str | None:
    """Expose only standard ONVIF scope paths, excluding query/fragment data."""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme != "onvif" or parsed.netloc.casefold() != "www.onvif.org" or not parsed.path:
            return None
        path = "/" + "/".join(part for part in parsed.path.split("/") if part)[:512]
        return urlunsplit(("onvif", "www.onvif.org", path, "", ""))
    except ValueError:
        return None


def parse_probe_response(data: bytes) -> list[DiscoveredDevice]:
    """Parse a bounded WS-Discovery ProbeMatches datagram without trusting XML input."""
    if not data or len(data) > MAX_DATAGRAM_BYTES:
        return []
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []

    devices: list[DiscoveredDevice] = []
    for match in (node for node in root.iter() if _local_name(node.tag) == "ProbeMatch"):
        address = next(
            (node.text.strip() for node in match.iter() if _local_name(node.tag) == "Address" and node.text and node.text.strip()),
            None,
        )
        if address is None:
            continue
        raw_uuid = address.removeprefix("urn:uuid:").removeprefix("uuid:")
        try:
            device_uuid = str(uuid.UUID(raw_uuid))
        except (ValueError, AttributeError):
            continue

        service_urls: list[str] = []
        scopes: list[str] = []
        for node in match.iter():
            name = _local_name(node.tag)
            if name == "XAddrs" and node.text:
                for candidate in node.text.split():
                    cleaned = _safe_service_url(candidate)
                    if cleaned and cleaned not in service_urls:
                        service_urls.append(cleaned)
            elif name == "Scopes" and node.text:
                for item in node.text.split()[:64]:
                    cleaned_scope = _safe_scope(item)
                    if cleaned_scope:
                        scopes.append(cleaned_scope)
        if service_urls:
            devices.append(DiscoveredDevice(device_uuid, tuple(service_urls[:8]), tuple(scopes[:64])))
    return devices


def _probe_message() -> bytes:
    message_id = f"urn:uuid:{uuid.uuid4()}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:a="http://www.w3.org/2005/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header><a:MessageID>{message_id}</a:MessageID>
  <a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>
  <a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>
 </e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>""".encode("utf-8")


def _discover_sync(timeout_seconds: float, interface_address: str | None) -> DiscoveryResult:
    if interface_address:
        parsed = ipaddress.ip_address(interface_address)
        if parsed.version != 4 or parsed.is_unspecified or parsed.is_multicast:
            raise ValueError("NVR_DISCOVERY_INTERFACE must be a unicast IPv4 address on the camera VLAN")

    devices: dict[str, DiscoveredDevice] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if interface_address:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_address))
        sock.bind((interface_address or "0.0.0.0", 0))
        sock.settimeout(0.25)
        sock.sendto(_probe_message(), (MULTICAST_ADDRESS, MULTICAST_PORT))
        deadline = time.monotonic() + timeout_seconds
        ignored_responses = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(min(0.25, remaining))
            try:
                data, _ = sock.recvfrom(MAX_DATAGRAM_BYTES + 1)
            except socket.timeout:
                continue
            except OSError:
                break
            matches = parse_probe_response(data)
            if not matches:
                ignored_responses += 1
            for device in matches:
                previous = devices.get(device.device_uuid)
                if previous:
                    urls = tuple(dict.fromkeys(previous.service_urls + device.service_urls))[:8]
                    scopes = tuple(dict.fromkeys(previous.scopes + device.scopes))[:64]
                    device = DiscoveredDevice(device.device_uuid, urls, scopes)
                devices[device.device_uuid] = device
    return DiscoveryResult(tuple(devices.values()), ignored_responses)


async def discover_onvif(timeout_seconds: float = DISCOVERY_TIMEOUT_SECONDS, interface_address: str | None = None) -> DiscoveryResult:
    bounded_timeout = min(max(float(timeout_seconds), 0.5), 15.0)
    return await asyncio.to_thread(_discover_sync, bounded_timeout, interface_address)
