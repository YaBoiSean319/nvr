from __future__ import annotations

import asyncio
import hashlib
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
IP_RANGE_MAX_HOSTS = 256
IP_RANGE_PORTS = (80, 8000, 8080, 8899)
IP_RANGE_CONCURRENCY = 64
IP_RANGE_PROBE_TIMEOUT_SECONDS = 1.25
PRIVATE_IPV4_NETWORKS = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


@dataclass(frozen=True)
class DiscoveredDevice:
    device_uuid: str
    service_urls: tuple[str, ...]
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryResult:
    devices: tuple[DiscoveredDevice, ...]
    ignored_responses: int
    targets_scanned: int = 0


def parse_camera_ip_range(value: str) -> tuple[ipaddress.IPv4Address, ...]:
    """Return bounded RFC1918 hosts only; range scans must never target public networks."""
    try:
        network = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as exc:
        raise ValueError("Enter a valid IPv4 CIDR range, such as 10.45.50.0/24") from exc
    if network.version != 4 or not any(network.subnet_of(allowed) for allowed in PRIVATE_IPV4_NETWORKS):
        raise ValueError("IP-range discovery is limited to private IPv4 camera networks")
    host_count = network.num_addresses - (2 if network.prefixlen < 31 else 0)
    if host_count < 1 or host_count > IP_RANGE_MAX_HOSTS:
        raise ValueError(f"IP-range discovery is limited to {IP_RANGE_MAX_HOSTS} addresses per scan")
    hosts = tuple(network.hosts())
    return hosts


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


_DEVICE_INFORMATION_SOAP = b'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
 <s:Body><tds:GetDeviceInformation/></s:Body>
</s:Envelope>'''


async def _probe_onvif_endpoint(address: ipaddress.IPv4Address, port: int, semaphore: asyncio.Semaphore) -> DiscoveredDevice | None:
    endpoint = f"http://{address}:{port}/onvif/device_service"
    request = (
        f"POST /onvif/device_service HTTP/1.1\r\n"
        f"Host: {address}:{port}\r\n"
        "Content-Type: application/soap+xml; charset=utf-8\r\n"
        'SOAPAction: "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation"\r\n'
        "Connection: close\r\n"
        f"Content-Length: {len(_DEVICE_INFORMATION_SOAP)}\r\n\r\n"
    ).encode("ascii") + _DEVICE_INFORMATION_SOAP
    try:
        async with semaphore:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(str(address), port), timeout=IP_RANGE_PROBE_TIMEOUT_SECONDS
            )
            try:
                writer.write(request)
                await writer.drain()
                response = await asyncio.wait_for(reader.read(MAX_DATAGRAM_BYTES + 1), timeout=IP_RANGE_PROBE_TIMEOUT_SECONDS)
            finally:
                writer.close()
                await writer.wait_closed()
    except (OSError, asyncio.TimeoutError):
        return None
    if not response or len(response) > MAX_DATAGRAM_BYTES:
        return None
    header, _, body = response.partition(b"\r\n\r\n")
    try:
        status = int(header.split(b"\r\n", 1)[0].split()[1])
    except (IndexError, ValueError):
        return None
    # A 401 from the standard ONVIF device-service path is useful: it confirms
    # the endpoint while allowing cameras that require ONVIF credentials.
    is_onvif = status == 401
    if status == 200 and body and b"<!DOCTYPE" not in body.upper() and b"<!ENTITY" not in body.upper():
        try:
            root = ET.fromstring(body)
            is_onvif = any(
                "onvif.org/ver10/device/wsdl" in node.tag
                or _local_name(node.tag) in {"GetDeviceInformationResponse", "Fault"}
                for node in root.iter()
            )
        except ET.ParseError:
            is_onvif = False
    if not is_onvif:
        return None
    # IP-scan identities are deterministic candidate keys, not camera UUIDs.
    candidate_key = "ipscan:" + hashlib.sha256(endpoint.encode("ascii")).hexdigest()[:32]
    return DiscoveredDevice(candidate_key, (endpoint,), ())


async def discover_onvif_ip_range(ip_range: str) -> DiscoveryResult:
    """Probe common ONVIF device-service ports over a small, private IPv4 range.

    This is unicast TCP/HTTP and works across routed networks when the NVR has
    an ACL-permitted route to the cameras. It does not rely on WS-Discovery
    multicast.
    """
    hosts = parse_camera_ip_range(ip_range)
    semaphore = asyncio.Semaphore(IP_RANGE_CONCURRENCY)

    async def probe_host(address: ipaddress.IPv4Address) -> DiscoveredDevice | None:
        probes = (_probe_onvif_endpoint(address, port, semaphore) for port in IP_RANGE_PORTS)
        for result in await asyncio.gather(*probes):
            if result is not None:
                return result
        return None

    results = await asyncio.gather(*(probe_host(address) for address in hosts))
    devices = tuple(device for device in results if device is not None)
    return DiscoveryResult(devices, 0, len(hosts))
