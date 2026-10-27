from __future__ import annotations

import asyncio
import random
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from .onvif_client import EVENTS, OnvifClient, OnvifError

WSNT = "http://docs.oasis-open.org/wsn/b-2"
EVENT_PORT = "http://www.onvif.org/ver10/events/wsdl/EventPortType/"  # The Events service's own operations are addressed through its port type, unlike the device and media services
MAX_XML_BYTES = 1024 * 1024
SUBSCRIPTION_SECONDS = 60
PULL_TIMEOUT_SECONDS = 20
MAX_BACKOFF_SECONDS = 30
MIN_PULL_SECONDS = 1.0  # A camera that ignores the pull timeout and answers at once is asked at most this often.
MAX_ACTIVE_SECONDS = 600  # A detector that never reports the end of motion does not hold a clip open for ever.
AUTH_RETRY_SECONDS = 300  # A refused login is retried rarely: repeated failures can lock a camera's account.
EVENTS_HTTP_TIMEOUT = httpx.Timeout(PULL_TIMEOUT_SECONDS + 10, connect=5.0)


@dataclass(frozen=True)
class CameraMotionEvent:
    active: bool
    event_time: datetime
    topic: str
    source: str = "onvif"
    source_key: str = ""  # which detector the camera named (a rule, a video source), so two detectors on one topic stay apart


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
        named = sorted(f"{node.attrib.get('Name', '')}={node.attrib.get('Value', '')}" for source in message.iter() if _local(source.tag) == "Source"
                       for node in source.iter() if _local(node.tag) == "SimpleItem")
        events.append(CameraMotionEvent(state, event_time, topic[:256], source_key=",".join(named)[:256]))
    return events


def normalise_topic(topic: str) -> str:
    """A topic without its namespace prefix and case, so ``tns1:RuleEngine/X`` and ``ruleengine/x`` are one topic."""
    return "/".join(part.rsplit(":", 1)[-1] for part in topic.strip().casefold().split("/"))


class MotionAggregator:
    """Turn every detector's changes into one motion state.

    Cameras often report the same movement on several topics (a cell detector and a video-source alarm, say). Motion starts when the first
    detector goes active and ends when the last one goes inactive, so repeated or overlapping reports make one transition, not many."""

    MAX_DETECTORS = 64

    def __init__(self) -> None:
        self.states: dict[tuple[str, str], bool] = {}
        self.active = False
        self.active_since = 0.0

    def reset(self) -> None:
        """Forget what was known (after a reconnect, when the camera restates its detectors)."""
        self.states.clear()
        self.active = False

    def feed(self, event: CameraMotionEvent) -> CameraMotionEvent | None:
        key = (normalise_topic(event.topic), event.source_key)
        if key not in self.states and len(self.states) >= self.MAX_DETECTORS:
            idle = next((known for known, state in self.states.items() if not state), None)
            if idle is None:
                return None
            del self.states[idle]  # Make room: a detector that is quiet is the cheapest to forget.
        self.states[key] = event.active
        now_active = any(self.states.values())
        if now_active == self.active:
            return None
        self.active = now_active
        self.active_since = time.monotonic()
        return CameraMotionEvent(now_active, event.event_time, event.topic, event.source, event.source_key)


def _address(root: ET.Element, parent: str) -> str | None:
    for node in root.iter():
        if _local(node.tag) == parent:
            address = next((child.text.strip() for child in node.iter() if _local(child.tag) == "Address" and child.text), None)
            if address:
                return address
    return None


def _reference_headers(root: ET.Element) -> str:
    """The WS-Addressing reference parameters a camera gave with its subscription address; it expects them back as SOAP headers."""
    for node in root.iter():
        if _local(node.tag) == "ReferenceParameters":
            return "".join(ET.tostring(child, encoding="unicode") for child in node)[:2048]
    return ""


def _granted_seconds(root: ET.Element) -> float:
    """How long the camera made the subscription last, from its answer; the time asked for if it does not say."""
    times = {_local(node.tag): _parse_datetime(node.text) for node in root.iter() if _local(node.tag) in {"CurrentTime", "TerminationTime"} and node.text}
    if len(times) == 2:
        granted = (times["TerminationTime"] - times["CurrentTime"]).total_seconds()
        if 2 <= granted <= 3600:
            return granted
    return float(SUBSCRIPTION_SECONDS)


def _status_for(error: OnvifError) -> str:
    return "authentication_error" if error.code == "auth_rejected" else "unsupported" if error.code == "unsupported" else "unavailable"


def _termination(seconds: int) -> str:
    return f"PT{seconds}S"


class PullPoint:
    """One live pull-point subscription on a camera: create it, pull events, renew it before it lapses, and end it."""

    def __init__(self, client: OnvifClient, events_url: str) -> None:
        self.client, self.events_url = client, events_url
        self.address: str | None = None
        self.headers = ""
        self.renew_at = 0.0

    async def create(self) -> None:
        root = await self.client.call(
            self.events_url, "CreatePullPointSubscription", EVENTS,
            f"<tev:CreatePullPointSubscription><tev:InitialTerminationTime>{_termination(SUBSCRIPTION_SECONDS)}</tev:InitialTerminationTime></tev:CreatePullPointSubscription>",
            service="events", action=EVENT_PORT + "CreatePullPointSubscriptionRequest")
        address = _address(root, "SubscriptionReference")
        if address is None:
            raise OnvifError("malformed", "The camera did not say where to pull events from")
        self.address = self.client.same_host(address)
        self.headers = _reference_headers(root)
        self.renew_at = time.monotonic() + _granted_seconds(root) / 2

    async def renew(self) -> None:
        await self.client.call(
            self.address, "Renew", WSNT,
            f"<wsnt:Renew xmlns:wsnt=\"{WSNT}\"><wsnt:TerminationTime>{_termination(SUBSCRIPTION_SECONDS)}</wsnt:TerminationTime></wsnt:Renew>",
            service="events", action="http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest", headers=self.headers)
        self.renew_at = time.monotonic() + SUBSCRIPTION_SECONDS / 2

    async def pull(self, seconds: int = PULL_TIMEOUT_SECONDS) -> list[CameraMotionEvent]:
        root = await self.client.call(
            self.address, "PullMessages", EVENTS,
            f"<tev:PullMessages><tev:Timeout>PT{seconds}S</tev:Timeout><tev:MessageLimit>100</tev:MessageLimit></tev:PullMessages>",
            service="events", action="http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest", headers=self.headers)
        return parse_motion_notifications(ET.tostring(root, encoding="utf-8"))

    async def end(self) -> None:
        """Tell the camera the subscription is finished, so it does not hold it until it lapses. Best effort."""
        if not self.address:
            return
        try:
            await asyncio.wait_for(self.client.call(
                self.address, "Unsubscribe", WSNT, f"<wsnt:Unsubscribe xmlns:wsnt=\"{WSNT}\"/>", service="events",
                action="http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest", headers=self.headers), timeout=3)
        except Exception:  # noqa: BLE001 - the subscription lapses by itself if this fails
            pass
        self.address = None


async def probe_motion(client: OnvifClient, events_url: str) -> dict[str, object]:
    """Try motion events the way recording will: subscribe, pull once, and end. ``{"ok": True, "topics": [...]}`` or ``{"ok": False, "reason": ...}``."""
    topics: list[str] = []
    try:
        root = await client.call(events_url, "GetEventProperties", EVENTS, "<tev:GetEventProperties/>", service="events", action=EVENT_PORT + "GetEventPropertiesRequest")
        topics = sorted(_topic_paths(root))[:50]
    except OnvifError:
        pass  # Optional: some cameras do not list their topics.
    point = PullPoint(client, events_url)
    try:
        await point.create()
        await point.pull(1)
    except OnvifError as error:
        return {"ok": False, "reason": error.message, "code": error.code}
    finally:
        await point.end()
    motion = [topic for topic in topics if "motion" in topic]
    if topics and not motion:
        return {"ok": False, "reason": "The camera offers events but none about motion", "code": "no_motion_topic"}
    return {"ok": True, "topics": motion}


def _topic_paths(root: ET.Element) -> set[str]:
    """Slash-separated topic paths from a TopicSet document: a topic is an element that is marked ``topic="true"``."""
    paths: set[str] = set()

    def walk(node: ET.Element, trail: tuple[str, ...]) -> None:
        for child in node:
            here = trail + (_local(child.tag),)
            if (child.attrib.get("{http://docs.oasis-open.org/wsn/t-1}topic") or child.attrib.get("topic") or "").casefold() == "true":
                paths.add("/".join(here).casefold())
            walk(child, here)

    for topic_set in (node for node in root.iter() if _local(node.tag) == "TopicSet"):
        walk(topic_set, ())
    return paths


async def _restart(aggregator: MotionAggregator, on_event: Callable[[CameraMotionEvent], Awaitable[None]]) -> None:
    """A new subscription restates the camera's detectors. Motion that was active before is ended, since its end may have been missed."""
    if aggregator.active:
        await on_event(CameraMotionEvent(False, datetime.now(timezone.utc), "onvif/subscription-restarted"))
    aggregator.reset()


async def subscribe_and_pull(
    device_service_url: str,
    username: str,
    password: str,
    on_event: Callable[[CameraMotionEvent], Awaitable[None]],
    on_status: Callable[[str], None],
    *,
    on_detail: Callable[[str], None] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Keep an ONVIF PullPoint subscription alive for as long as the task runs.

    The subscription is renewed before it lapses and made again if the camera drops it. After a fault the NVR reconnects with a growing, bounded,
    jittered wait, and reports ``healthy``, ``unavailable``, ``authentication_error`` or ``unsupported`` for the camera's motion source. Detectors that
    report the same movement are merged into one motion state. ``on_detail`` receives a short, credential-free sentence saying what is wrong
    (or an empty string when all is well), so a person can act on it. A source is ``healthy`` only once events have actually been pulled."""
    detail = on_detail or (lambda _text: None)
    parsed = urlsplit(device_service_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        detail("The ONVIF event address is not valid")
        on_status("configuration_error")
        return
    try:
        OnvifClient(parsed.hostname, username, password)
    except ValueError as exc:
        detail(str(exc))
        on_status("configuration_error")
        return
    backoff = 1.0
    aggregator = MotionAggregator()
    while True:
        delay = None
        try:
            async with OnvifClient(parsed.hostname, username, password, timeout=EVENTS_HTTP_TIMEOUT, transport=transport) as client:
                url = await client.locate_device(device_service_url)
                services = await client.services(url)
                events_url = services.urls.get("events")
                if events_url is None:
                    detail("The camera has no ONVIF Events service")
                    on_status("unsupported")
                    return
                point = PullPoint(client, events_url)
                try:
                    await point.create()
                    await _restart(aggregator, on_event)
                    first = True
                    while True:
                        if time.monotonic() >= point.renew_at:
                            try:
                                await point.renew()
                            except OnvifError:
                                # The camera forgot the subscription (a restart, a lapse): end the old one if it can be, and make a new one rather than back off.
                                await point.end()
                                await point.create()
                                await _restart(aggregator, on_event)
                        started = time.monotonic()
                        pulled = await point.pull(1 if first else PULL_TIMEOUT_SECONDS)
                        if first:
                            first = False
                            detail("")
                            on_status("healthy")
                            backoff = 1.0
                        for event in pulled:
                            merged = aggregator.feed(event)
                            if merged is not None:
                                await on_event(merged)
                        if aggregator.active and time.monotonic() - aggregator.active_since > MAX_ACTIVE_SECONDS:
                            await _restart(aggregator, on_event)  # Motion that has lasted this long is a detector that lost its end.
                        if not pulled and time.monotonic() - started < MIN_PULL_SECONDS:
                            await asyncio.sleep(MIN_PULL_SECONDS - (time.monotonic() - started))
                finally:
                    await point.end()
        except asyncio.CancelledError:
            raise
        except OnvifError as error:
            detail(error.message)
            on_status(_status_for(error))
            delay = AUTH_RETRY_SECONDS if error.code == "auth_rejected" else None
        except (httpx.HTTPError, OSError, ValueError, ET.ParseError):
            detail("The camera's event service could not be reached")
            on_status("unavailable")
        except Exception:  # noqa: BLE001 - one camera's fault, such as a failure to store an event, must not end its motion source
            detail("Motion events could not be handled")
            on_status("unavailable")
        if aggregator.active:
            # The camera is out of reach, so the end of any motion is unknown: end it rather than leave a clip open until the camera is back.
            await on_event(CameraMotionEvent(False, datetime.now(timezone.utc), "onvif/subscription-lost"))
            aggregator.reset()
        await asyncio.sleep(delay or backoff * random.uniform(0.8, 1.2))
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
