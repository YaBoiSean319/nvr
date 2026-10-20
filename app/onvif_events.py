from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

SOAP = "http://www.w3.org/2003/05/soap-envelope"
ADDRESSING = "http://www.w3.org/2005/08/addressing"
DEVICE = "http://www.onvif.org/ver10/device/wsdl"
EVENTS = "http://www.onvif.org/ver10/events/wsdl"
MAX_XML_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CameraMotionEvent:
    active: bool
    event_time: datetime
    topic: str


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_xml(data: bytes) -> ET.Element:
    if not data or len(data) > MAX_XML_BYTES:
        raise ValueError("ONVIF response size is invalid")
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("ONVIF response XML is unsafe")
    return ET.fromstring(data)


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_motion_notifications(data: bytes) -> list[CameraMotionEvent]:
    """Extract standard ONVIF motion state changes without trusting XML text."""
    root = _parse_xml(data)
    events: list[CameraMotionEvent] = []
    for notification in (node for node in root.iter() if _local(node.tag) == "NotificationMessage"):
        topic = next((node.text.strip() for node in notification.iter() if _local(node.tag) == "Topic" and node.text), "")
        topic_folded = topic.casefold()
        if not any(token in topic_folded for token in ("motion", "cellmotion", "motionalarm")):
            continue
        messages = [node for node in notification.iter() if _local(node.tag) == "Message"]
        message = next((node for node in messages if node.attrib.get("UtcTime")), messages[-1] if messages else None)
        if message is None:
            continue
        state: bool | None = None
        for node in message.iter():
            if _local(node.tag) != "SimpleItem":
                continue
            name = (node.attrib.get("Name") or "").casefold()
            value = (node.attrib.get("Value") or "").casefold()
            if name in {"ismotion", "motion", "state", "active"}:
                if value in {"true", "1", "yes", "active"}:
                    state = True
                elif value in {"false", "0", "no", "inactive"}:
                    state = False
                if state is not None:
                    break
        if state is None:
            # Some event producers encode the state as a direct element.
            for node in message.iter():
                if _local(node.tag).casefold() in {"ismotion", "motion", "state", "active"} and node.text:
                    value = node.text.strip().casefold()
                    if value in {"true", "1", "yes", "active"}:
                        state = True
                    elif value in {"false", "0", "no", "inactive"}:
                        state = False
                    if state is not None:
                        break
        if state is None:
            continue
        event_time = _parse_datetime(message.attrib.get("UtcTime"))
        events.append(CameraMotionEvent(state, event_time, topic[:256]))
    return events


def _soap(action: str, body: str) -> bytes:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP}" xmlns:a="{ADDRESSING}" xmlns:td="{DEVICE}" xmlns:tev="{EVENTS}">
 <s:Header><a:Action s:mustUnderstand="1">{action}</a:Action></s:Header>
 <s:Body>{body}</s:Body>
</s:Envelope>'''.encode()


def _endpoint(root: ET.Element, name: str) -> str | None:
    for node in root.iter():
        if _local(node.tag) == name and node.text:
            candidate = node.text.strip()
            parsed = urlsplit(candidate)
            if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
                return candidate
    return None


async def _post(client: httpx.AsyncClient, endpoint: str, action: str, body: str) -> ET.Element:
    response = await client.post(endpoint, content=_soap(action, body), headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'})
    response.raise_for_status()
    return _parse_xml(response.content)


async def subscribe_and_pull(
    device_service_url: str,
    username: str,
    password: str,
    on_event: Callable[[CameraMotionEvent], Awaitable[None]],
    on_status: Callable[[str], None],
) -> None:
    """Keep an ONVIF PullPoint subscription alive; reconnect after transport/device faults."""
    parsed = urlsplit(device_service_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        on_status("configuration_error")
        return
    auth: httpx.Auth | None = httpx.DigestAuth(username, password) if username else None
    backoff = 1
    while True:
        try:
            async with httpx.AsyncClient(auth=auth, timeout=httpx.Timeout(25, connect=5), trust_env=False) as client:
                capabilities = await _post(
                    client, device_service_url, "http://www.onvif.org/ver10/device/wsdl/GetCapabilities",
                    "<td:GetCapabilities><td:Category>Events</td:Category></td:GetCapabilities>",
                )
                events_url = None
                for node in capabilities.iter():
                    if _local(node.tag) == "Events":
                        events_url = _endpoint(node, "XAddr")
                        if events_url:
                            break
                if events_url is None:
                    on_status("unsupported")
                    return
                subscription = await _post(
                    client, events_url, "http://www.onvif.org/ver10/events/wsdl/EventPortType/CreatePullPointSubscription",
                    "<tev:CreatePullPointSubscription><tev:InitialTerminationTime>PT60S</tev:InitialTerminationTime></tev:CreatePullPointSubscription>",
                )
                pull_url = None
                for node in subscription.iter():
                    if _local(node.tag) == "SubscriptionReference":
                        pull_url = _endpoint(node, "Address")
                        if pull_url:
                            break
                if pull_url is None:
                    raise ValueError("ONVIF subscription endpoint was not returned")
                on_status("healthy")
                backoff = 1
                while True:
                    response = await _post(
                        client, pull_url, "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessages",
                        "<tev:PullMessages><tev:Timeout>PT20S</tev:Timeout><tev:MessageLimit>100</tev:MessageLimit></tev:PullMessages>",
                    )
                    for event in parse_motion_notifications(ET.tostring(response, encoding="utf-8")):
                        await on_event(event)
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            # Status code only; response body and URL may contain credentials or device details.
            on_status("authentication_error" if exc.response.status_code in {401, 403} else "unavailable")
        except (httpx.HTTPError, OSError, ValueError, ET.ParseError):
            on_status("unavailable")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)
