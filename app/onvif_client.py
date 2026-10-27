"""A resilient ONVIF client: authentication that is negotiated, services that are discovered, and failures that say what happened.

Cameras differ in what each ONVIF service asks for. A Hanwha camera, for one, answers the Media service over HTTP Digest while its
Events service wants a WS-Security ``UsernameToken`` with a ``PasswordDigest``. So the client does not assume one scheme: for each service
it tries the request without credentials, then with HTTP Digest, then with WS-Security, then with both, and remembers the first that the
service accepted. A camera whose clock differs from the NVR's rejects a WS-Security token stamped with the NVR's time, so the camera's
clock (``GetSystemDateAndTime``) is read first and the token stamped with the camera's time.

Every request is noted in ``trace``: the operation, the service, the authentication method, the outcome and the HTTP status. The trace
never holds a username, a password, a nonce, a token or a response body; neither do the errors, whose messages are fixed phrases that
tell an administrator what to do. Service addresses a camera returns are kept on the address the administrator entered (only the
scheme, port and path are taken from them), so a camera cannot point the NVR at another host.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import httpx

from .onvif_profiles import MediaProfile, parse_profiles

SOAP = "http://www.w3.org/2003/05/soap-envelope"
ADDRESSING = "http://www.w3.org/2005/08/addressing"
DEVICE = "http://www.onvif.org/ver10/device/wsdl"
MEDIA = "http://www.onvif.org/ver10/media/wsdl"
MEDIA2 = "http://www.onvif.org/ver20/media/wsdl"
EVENTS = "http://www.onvif.org/ver10/events/wsdl"
SCHEMA = "http://www.onvif.org/ver10/schema"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PASSWORD_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
BASE64_BINARY = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_PORTS = (80, 8000, 8080, 8899)
TIMEOUT = httpx.Timeout(float(os.environ.get("NVR_ONVIF_TIMEOUT_SECONDS", "8")), connect=3.0)
AUTH_ORDER = ("none", "digest", "wsse", "digest+wsse")

SERVICE_NAMESPACES = {
    DEVICE: "device", MEDIA: "media", MEDIA2: "media2", EVENTS: "events", "http://www.onvif.org/ver20/ptz/wsdl": "ptz",
    "http://www.onvif.org/ver20/imaging/wsdl": "imaging", "http://www.onvif.org/ver20/analytics/wsdl": "analytics",
    "http://www.onvif.org/ver10/deviceIO/wsdl": "deviceio", "http://www.onvif.org/ver10/recording/wsdl": "recording",
    "http://www.onvif.org/ver10/replay/wsdl": "replay", "http://www.onvif.org/ver10/search/wsdl": "search",
}
CAPABILITY_TAGS = {"Device": "device", "Media": "media", "Events": "events", "PTZ": "ptz", "Imaging": "imaging", "Analytics": "analytics"}

ERROR_TEXT = {
    "unreachable": "The camera could not be reached: check its address and that it is on and on the network",
    "timeout": "The camera did not answer in time",
    "auth_rejected": "Camera authentication rejected: check the username and password (the camera refused HTTP Digest and WS-Security logins)",
    "service_unavailable": "The camera does not offer that ONVIF service at the address it reported",
    "soap_fault": "The camera refused the request",
    "malformed": "The camera answered, but not with a valid ONVIF response",
    "unsupported": "The camera answered in a way the NVR does not support",
}


class OnvifError(Exception):
    """A failed ONVIF step. ``code`` is one of ``ERROR_TEXT``; ``message`` is safe to show and never holds a credential."""

    def __init__(self, code: str, message: str | None = None, *, step: str = "", status: int | None = None) -> None:
        self.code, self.step, self.status = code, step, status
        self.message = message or ERROR_TEXT.get(code, "The ONVIF request failed")
        super().__init__(self.message)


@dataclass
class DeviceInfo:
    manufacturer: str = ""
    model: str = ""
    firmware: str = ""


@dataclass
class Services:
    urls: dict[str, str] = field(default_factory=dict)
    source: str = ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xml(data: bytes) -> ET.Element:
    if not data or len(data) > MAX_RESPONSE_BYTES:
        raise OnvifError("malformed")
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise OnvifError("malformed")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise OnvifError("malformed") from None


def normalise_host(value: str) -> str:
    """The camera address entered by an administrator as an IPv4/IPv6 address or a host name; ValueError if it is neither."""
    text = (value or "").strip().strip("[]")
    if not text or len(text) > 253:
        raise ValueError("Enter the camera's IP address or host name")
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*", text):
            raise ValueError("Enter the camera's IP address or host name") from None
        return text.lower()
    if address.is_unspecified or address.is_multicast or address.is_link_local:
        raise ValueError("Enter a unicast camera address")
    return str(address)


def _netloc(host: str, port: int | None) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"{shown}:{port}" if port else shown


def _escape_attr(value: str) -> str:
    return escape(value, {'"': "&quot;"})


class OnvifClient:
    """One camera's ONVIF services. Make one per camera per operation; it holds what it learned (clock offset, authentication) in memory only."""

    def __init__(self, host: str, username: str = "", password: str = "", *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: httpx.Timeout = TIMEOUT) -> None:
        self.host = normalise_host(host)
        if bool(username) != bool(password):
            raise ValueError("Enter both the camera username and password, or leave both blank")
        self._username, self._password = username, password
        self._http = httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False, transport=transport)
        self.clock_offset: timedelta | None = None
        self.auth_by_service: dict[str, str] = {}
        self._digests: dict[str, httpx.DigestAuth] = {}  # One for each service address: after its first challenge it answers the next requests without another round trip.
        self.trace: list[dict[str, Any]] = []
        self._refused = False
        self._device_url: str | None = None

    async def __aenter__(self) -> "OnvifClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    # ----------------------------------------------------------------------------------------------------------------- transport

    def same_host(self, url: str) -> str:
        """An address a camera returned (a subscription, say), moved onto the host the administrator entered. The query is kept: some cameras name the subscription in it."""
        return self._kept(url, query=True)

    def _kept(self, url: str, query: bool = False) -> str:
        """An address a camera returned, moved onto the host the administrator entered."""
        parts = urlsplit(url.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise OnvifError("unsupported", "The camera reported an invalid service address")
        try:
            port = parts.port
        except ValueError:
            raise OnvifError("unsupported", "The camera reported an invalid service port") from None
        return urlunsplit((parts.scheme, _netloc(self.host, port), parts.path, parts.query[:256] if query else "", ""))

    def _security_header(self) -> str:
        nonce = os.urandom(16)
        now = datetime.now(timezone.utc) + (self.clock_offset or timedelta())
        created = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self._password.encode()).digest()).decode()  # noqa: S324 - ONVIF's PasswordDigest is SHA-1
        return (f'<wsse:Security s:mustUnderstand="1" xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}"><wsse:UsernameToken>'
                f"<wsse:Username>{escape(self._username)}</wsse:Username>"
                f'<wsse:Password Type="{PASSWORD_DIGEST}">{digest}</wsse:Password>'
                f'<wsse:Nonce EncodingType="{BASE64_BINARY}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
                f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>")

    def _envelope(self, action: str, body: str, *, wsse: bool, extra: str = "") -> bytes:
        header = f'<a:Action s:mustUnderstand="1">{action}</a:Action>' + extra + (self._security_header() if wsse else "")
        return (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{SOAP}" xmlns:a="{ADDRESSING}" xmlns:tds="{DEVICE}" xmlns:trt="{MEDIA}" '
                f'xmlns:tr2="{MEDIA2}" xmlns:tev="{EVENTS}" xmlns:tt="{SCHEMA}"><s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>').encode()

    def _note(self, **entry: Any) -> None:
        self.trace.append(entry)
        del self.trace[:-200]

    def _fault(self, root: ET.Element | None) -> tuple[str, str] | None:
        """The (code, reason) of a SOAP fault, or None when the document is not one."""
        if root is None:
            return None
        fault = next((node for node in root.iter() if _local(node.tag) == "Fault"), None)
        if fault is None:
            return None
        values = [node.text.strip() for node in fault.iter() if _local(node.tag) in {"Value", "faultcode"} and node.text]
        reasons = [node.text.strip() for node in fault.iter() if _local(node.tag) in {"Text", "faultstring"} and node.text]
        return self._scrub(" ".join(values))[:200], self._scrub(reasons[0] if reasons else "")[:200]

    def _scrub(self, text: str) -> str:
        """Camera-supplied text with the login removed, since some firmware repeats the submitted user or password in a fault."""
        for secret in (self._password, self._username):
            if secret:
                text = text.replace(secret, "***")
        return text

    @staticmethod
    def _is_auth_fault(code: str, reason: str) -> bool:
        text = f"{code} {reason}".casefold()
        return any(token in text for token in ("notauthorized", "not authorized", "authorization", "authentication", "unauthorized", "sender not authorized", "failedauthentication"))

    async def _once(self, url: str, action: str, body: str, method: str, extra: str = "") -> tuple[httpx.Response, ET.Element | None]:
        auth = self._digests.setdefault(url, httpx.DigestAuth(self._username, self._password)) if "digest" in method else None
        envelope = self._envelope(action, body, wsse="wsse" in method, extra=extra)
        try:
            response = await self._http.post(url, content=envelope, auth=auth, headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'})
        except httpx.TimeoutException:
            raise OnvifError("timeout") from None
        except (httpx.HTTPError, OSError):
            raise OnvifError("unreachable") from None
        root = None
        if response.content and len(response.content) <= MAX_RESPONSE_BYTES:
            try:
                root = parse_xml(response.content)
            except OnvifError:
                root = None
            # A well-formed web page is not an ONVIF answer: only a SOAP envelope is.
            if root is not None and _local(root.tag) != "Envelope":
                root = None
        return response, root

    def _methods(self, service: str) -> list[str]:
        if not self._username:
            return ["none"]
        known = self.auth_by_service.get(service)
        return [known, *[item for item in AUTH_ORDER if item != known]] if known else list(AUTH_ORDER)

    async def call(self, url: str, operation: str, namespace: str, body: str, *, service: str, action: str | None = None, headers: str = "") -> ET.Element:
        """Send one SOAP operation, negotiating authentication, and return the response document. ``OnvifError`` says why it failed.

        ``action`` is the WS-Addressing action where it is not ``namespace/operation`` (subscription management uses another namespace).
        ``headers`` is extra SOAP header XML, such as the reference parameters a camera gave with a subscription address."""
        action = action or f"{namespace}/{operation}"
        rejected = False
        if self._refused and service not in self.auth_by_service:
            # Every login method was already refused: asking again only counts towards a camera's account lock-out.
            raise OnvifError("auth_rejected", step=operation)
        queue = self._methods(service)
        while queue:
            method = queue.pop(0)
            if "wsse" in method and self.clock_offset is None:
                await self._sync_clock()
            started = time.monotonic()
            try:
                response, root = await self._once(url, action, body, method, headers)
            except OnvifError as exc:
                self._note(operation=operation, service=service, auth=method, outcome=exc.code, status=None, ms=int((time.monotonic() - started) * 1000))
                raise
            fault = self._fault(root)
            status = response.status_code
            elapsed = int((time.monotonic() - started) * 1000)
            if status in {401, 403} or (fault and self._is_auth_fault(*fault)):
                rejected = True
                self._note(operation=operation, service=service, auth=method, outcome="auth_failed", status=status, ms=elapsed)
                challenge = response.headers.get("www-authenticate", "").casefold()
                if "digest" not in method and challenge and "digest" not in challenge:
                    # The camera named another scheme and not Digest, so Digest would send what was just refused: go on to WS-Security.
                    queue = [item for item in queue if "digest" not in item]
                continue
            if status == 404:
                self._note(operation=operation, service=service, auth=method, outcome="service_unavailable", status=status, ms=elapsed)
                raise OnvifError("service_unavailable", step=operation, status=status)
            if 300 <= status < 400:
                self._note(operation=operation, service=service, auth=method, outcome="unsupported", status=status, ms=elapsed)
                raise OnvifError("unsupported", step=operation, status=status)
            if fault:
                self._note(operation=operation, service=service, auth=method, outcome="soap_fault", status=status, ms=elapsed, fault=fault[0])
                raise OnvifError("soap_fault", f"The camera refused {operation}" + (f": {fault[1]}" if fault[1] else ""), step=operation, status=status)
            if status >= 400:
                self._note(operation=operation, service=service, auth=method, outcome="http_error", status=status, ms=elapsed)
                raise OnvifError("service_unavailable", f"The camera answered {operation} with HTTP {status}", step=operation, status=status)
            if root is None:
                self._note(operation=operation, service=service, auth=method, outcome="malformed", status=status, ms=elapsed)
                raise OnvifError("malformed", step=operation, status=status)
            self.auth_by_service[service] = method
            self._note(operation=operation, service=service, auth=method, outcome="ok", status=status, ms=elapsed)
            return root
        if rejected and not self._username:
            raise OnvifError("auth_rejected", "The camera requires a username and password", step=operation)
        self._refused = self._refused or (rejected and bool(self._username))
        raise OnvifError("auth_rejected" if rejected else "unreachable", step=operation)

    async def _sync_clock(self) -> None:
        """Read the camera's clock so a WS-Security token is stamped with the camera's time; a camera that will not say is taken to agree."""
        self.clock_offset = timedelta()
        url = self._device_url
        if not url:
            return
        try:
            root = await self._clock_call(url)
        except OnvifError:
            return
        utc = next((node for node in root.iter() if _local(node.tag) == "UTCDateTime"), None)
        if utc is None:
            return
        try:
            fields = {_local(child.tag): int(child.text) for part in utc.iter() for child in part if child.text and child.text.strip().isdigit()}
            camera = datetime(fields["Year"], fields["Month"], fields["Day"], fields["Hour"], fields["Minute"], fields["Second"], tzinfo=timezone.utc)
        except (KeyError, ValueError):
            return
        self.clock_offset = camera - datetime.now(timezone.utc)
        self._note(operation="GetSystemDateAndTime", service="device-time", auth="none", outcome="ok", status=200, ms=0, clock_offset_seconds=round(self.clock_offset.total_seconds()))

    async def _clock_call(self, url: str) -> ET.Element:
        response, root = await self._once(url, f"{DEVICE}/GetSystemDateAndTime", "<tds:GetSystemDateAndTime/>", "none")
        if response.status_code != 200 or root is None:
            raise OnvifError("unsupported")
        return root

    # --------------------------------------------------------------------------------------------------------------------- device

    async def locate_device(self, device_service_url: str | None = None, ports: tuple[int, ...] = DEFAULT_PORTS) -> str:
        """The camera's ONVIF device service address: the one given, or the first port that answers like an ONVIF device."""
        if device_service_url:
            candidates = [self._kept(device_service_url)]
        else:
            candidates = [f"http://{_netloc(self.host, port)}/onvif/device_service" for port in ports]
        refused = 0
        for url in candidates:
            started = time.monotonic()
            try:
                response, root = await self._once(url, f"{DEVICE}/GetSystemDateAndTime", "<tds:GetSystemDateAndTime/>", "none")
            except OnvifError as exc:
                refused += exc.code == "unreachable"
                self._note(operation="Locate", service="device", auth="none", outcome=exc.code, status=None, ms=int((time.monotonic() - started) * 1000), port=urlsplit(url).port)
                continue
            # Any SOAP answer, even a fault that wants credentials, shows an ONVIF device is here. At an address the administrator gave, any HTTP answer will do:
            # a wrong path is reported by the first real request.
            if device_service_url or root is not None and any(_local(node.tag) in {"Envelope", "Fault"} for node in root.iter()):
                self._note(operation="Locate", service="device", auth="none", outcome="ok", status=response.status_code, ms=int((time.monotonic() - started) * 1000), port=urlsplit(url).port)
                self._device_url = url
                return url
            self._note(operation="Locate", service="device", auth="none", outcome="not_onvif", status=response.status_code, ms=int((time.monotonic() - started) * 1000), port=urlsplit(url).port)
        if device_service_url:
            raise OnvifError("unreachable", f"No ONVIF service answered at the device service address for {self.host}: check the address, that ONVIF is enabled on the camera, and the ONVIF port")
        raise OnvifError("unreachable", f"No ONVIF service answered at {self.host} on port {', '.join(str(port) for port in ports)}: check the address, that ONVIF is enabled on the camera, and the ONVIF port")

    async def device_information(self, device_url: str) -> DeviceInfo:
        self._device_url = device_url
        root = await self.call(device_url, "GetDeviceInformation", DEVICE, "<tds:GetDeviceInformation/>", service="device")
        value = lambda name: next((node.text.strip() for node in root.iter() if _local(node.tag) == name and node.text), "")[:128]  # noqa: E731
        return DeviceInfo(value("Manufacturer"), value("Model"), value("FirmwareVersion"))

    async def services(self, device_url: str) -> Services:
        """Every service the camera offers, by name: from GetServices, or GetCapabilities where the camera has no GetServices."""
        self._device_url = device_url
        found: dict[str, str] = {"device": device_url}
        try:
            root = await self.call(device_url, "GetServices", DEVICE, "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>", service="device")
            for service in (node for node in root.iter() if _local(node.tag) == "Service"):
                namespace = next((child.text.strip() for child in service if _local(child.tag) == "Namespace" and child.text), "")
                address = next((child.text.strip() for child in service if _local(child.tag) == "XAddr" and child.text), "")
                if namespace in SERVICE_NAMESPACES and address:
                    found.setdefault(SERVICE_NAMESPACES[namespace], self._kept(address))
            if len(found) > 1:
                return Services(found, "GetServices")
        except OnvifError as exc:
            if exc.code in {"auth_rejected", "unreachable", "timeout"}:
                raise
        for category in ("All", "Media", "Events", "PTZ", "Analytics"):
            try:
                root = await self.call(device_url, "GetCapabilities", DEVICE, f"<tds:GetCapabilities><tds:Category>{category}</tds:Category></tds:GetCapabilities>", service="device")
            except OnvifError as exc:
                if exc.code in {"auth_rejected", "unreachable", "timeout"}:
                    raise
                continue
            for node in root.iter():
                name = CAPABILITY_TAGS.get(_local(node.tag))
                address = next((child.text.strip() for child in node if _local(child.tag) == "XAddr" and child.text), "") if name else ""
                if name and address:
                    found.setdefault(name, self._kept(address))
            if len(found) > 1 and category == "All":
                break
        return Services(found, "GetCapabilities")

    # ---------------------------------------------------------------------------------------------------------------------- media

    async def profiles(self, services: Services) -> list[MediaProfile]:
        """The camera's video profiles from Media2 where it has it, else Media1; the error says which service failed."""
        failures: list[OnvifError] = []
        if "media2" in services.urls:
            try:
                root = await self.call(services.urls["media2"], "GetProfiles", MEDIA2, "<tr2:GetProfiles><tr2:Type>All</tr2:Type></tr2:GetProfiles>", service="media2")
                found = parse_profiles(root, "media2")
                if found:
                    return found
            except OnvifError as exc:
                if exc.code in {"auth_rejected", "unreachable", "timeout"}:
                    raise
                failures.append(exc)
        if "media" in services.urls:
            root = await self.call(services.urls["media"], "GetProfiles", MEDIA, "<trt:GetProfiles/>", service="media")
            return parse_profiles(root, "media1")
        if failures:
            raise failures[0]
        raise OnvifError("service_unavailable", "ONVIF Media service unavailable: the camera did not report one")

    async def stream_uri(self, services: Services, profile: MediaProfile) -> str:
        """The profile's RTSP address as the camera reports it (no credentials in it)."""
        token = escape(profile.token)
        if profile.source == "media2" and "media2" in services.urls:
            root = await self.call(services.urls["media2"], "GetStreamUri", MEDIA2, f"<tr2:GetStreamUri><tr2:Protocol>RTSP</tr2:Protocol><tr2:ProfileToken>{token}</tr2:ProfileToken></tr2:GetStreamUri>", service="media2")
        elif "media" in services.urls:
            body = ("<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                    f"<trt:ProfileToken>{token}</trt:ProfileToken></trt:GetStreamUri>")
            root = await self.call(services.urls["media"], "GetStreamUri", MEDIA, body, service="media")
        else:
            raise OnvifError("service_unavailable", "ONVIF Media service unavailable: the camera did not report one")
        uri = next((node.text.strip() for node in root.iter() if _local(node.tag) == "Uri" and node.text), "")
        if not uri:
            raise OnvifError("malformed", "The camera did not return a stream address for that profile")
        return uri

    def stream_url_with_login(self, uri: str) -> str:
        """The RTSP address for FFmpeg: the camera's path and port, the host the administrator entered, and the login encoded into it."""
        parts = urlsplit(uri.strip())
        if parts.scheme not in {"rtsp", "rtsps"} or not parts.hostname:
            raise OnvifError("unsupported", "The camera did not return a valid RTSP stream address")
        try:
            port = parts.port
        except ValueError:
            raise OnvifError("unsupported", "The camera returned an invalid RTSP port") from None
        if any(char.isspace() or char in "'\\\x00" for char in parts.path + parts.query):
            raise OnvifError("unsupported", "The camera returned an invalid RTSP address")
        login = f"{quote(self._username, safe='')}:{quote(self._password, safe='')}@" if self._username else ""
        return urlunsplit((parts.scheme, f"{login}{_netloc(self.host, port)}", parts.path, parts.query, ""))

    async def service_capabilities(self, services: Services, name: str) -> dict[str, str]:
        """A service's own capability attributes (for example whether Events offers a pull point), or {} when it will not say."""
        namespaces = {"events": (EVENTS, "tev"), "media2": (MEDIA2, "tr2"), "media": (MEDIA, "trt")}
        if name not in services.urls or name not in namespaces:
            return {}
        namespace, prefix = namespaces[name]
        try:
            root = await self.call(services.urls[name], "GetServiceCapabilities", namespace, f"<{prefix}:GetServiceCapabilities/>", service=name)
        except OnvifError as exc:
            if exc.code in {"auth_rejected", "unreachable", "timeout"}:
                raise
            return {}
        node = next((item for item in root.iter() if _local(item.tag) == "Capabilities"), None)
        return {_local(key): value for key, value in node.attrib.items()} if node is not None else {}

    def diagnostics(self) -> list[dict[str, Any]]:
        """The trace, safe to show or export: operations, outcomes, statuses and timings, no credentials and no response bodies."""
        return [dict(entry) for entry in self.trace]

