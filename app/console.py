"""Multi-site console (#141): other NVRs (sites) federated into this one through scoped API tokens.

A site keeps its recording, storage and configuration; the console holds how to reach it (an address and a token limited to the ``read`` and ``media`` scopes,
stored encrypted and never returned) and nothing of its footage. Everything a site owns appears here under an identifier of the form ``<site>~<id at that site>``,
so the interface that shows this NVR's own cameras shows the sites' as well: live and playback are proxied through the console to the site, and the camera list,
camera groups (one per site), alarms, playback timelines and attribute search are asked of every site the person may see, concurrently, and merged.

A site that does not answer is marked unreachable at once and left alone for ``RETRY_AFTER`` seconds before one request tries it again, so one site being down never
makes the console wait for it again and again: the others keep answering at their own speed. A site is never asked about anything the person has not been
granted (a grant for a site, or for every camera).
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import typing
import logging
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from .access import SITE_SEPARATOR as SEP, site_of
from .credentials import decrypt, encrypt
from .store import connect
from .webhooks import validate_target_url

logger = logging.getLogger("nvr.console")

RETRY_AFTER = 10.0        # seconds a site that failed is left alone before one request tries it again
HEALTH_SECONDS = 15.0     # how often every site's health is refreshed
INVENTORY_SECONDS = 4.0   # a site's camera list is reused for this long
FAN_OUT_SECONDS = 8.0     # no site makes a merged answer wait longer than this
TIMEOUT = httpx.Timeout(6.0, connect=2.0)
MAX_SITES = 50
SITE_ID = re.compile(r"^[a-z0-9]{8}$")
NEEDED_SCOPES = ("read", "media")
HIDDEN_CAMERA_FIELDS = ("camera_ip", "device_service_url", "onvif_profile_token", "live_substream_profile_token")


class ConsoleError(Exception):
    """A site cannot be saved as given (the message is for the administrator)."""


class SiteDown(Exception):
    """The site did not answer (or is being left alone after not answering)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def prefixed(site_id: str, local_id: str) -> str:
    return f"{site_id}{SEP}{local_id}"


def split_id(identifier: str) -> tuple[str, str] | None:
    site, separator, local = identifier.partition(SEP)
    return (site, local) if separator and site and local else None


# --- a site as the console keeps it -----------------------------------------------------------------------------------------------------------

@dataclass
class SiteLink:
    id: str
    name: str
    base_url: str
    token: str
    enabled: bool
    fingerprint: tuple
    client: httpx.AsyncClient
    retry_at: float = 0.0
    last_contact_at: str | None = None
    last_checked_at: str | None = None
    last_error: str | None = None
    refused: bool = False
    camera_count: int | None = None
    open_alarms: int | None = None
    cameras: list[dict[str, Any]] = field(default_factory=list)
    cameras_at: float = 0.0
    persisted_at: float = 0.0

    @property
    def down(self) -> bool:
        return time.monotonic() < self.retry_at

    @property
    def state(self) -> str:
        return "disabled" if not self.enabled else "unreachable" if self.down else "refused" if self.refused else "ok"

    def failed(self, reason: str) -> None:
        self.retry_at = time.monotonic() + RETRY_AFTER
        self.last_error = reason
        self.last_checked_at = _now_iso()

    def answered(self, refused: bool = False) -> None:
        self.retry_at = 0.0
        self.refused = refused
        self.last_contact_at = self.last_checked_at = _now_iso()
        self.last_error = "The site refused the console's token" if refused else None


class Registry:
    """The sites, loaded from the database and kept with the state of the last contact. One per process."""

    def __init__(self) -> None:
        self.links: dict[str, SiteLink] = {}
        self.enabled = False
        self.loaded_at = 0.0
        self.lock = asyncio.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None

    def invalidate(self) -> None:
        self.loaded_at = 0.0

    def _same_loop(self) -> None:
        """Connections belong to the event loop that opened them. There is one loop in service; a test that drives the application from several (a new one for each
        request of a client) gets fresh connections for each, with what was learned about the sites kept."""
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            if self.loop is not None:
                for link in self.links.values():
                    link.client = _client(link.base_url, link.token)
                self.lock = asyncio.Lock()
            self.loop = loop

    async def load(self, max_age: float = 2.0) -> None:
        self._same_loop()
        if time.monotonic() - self.loaded_at < max_age:
            return
        async with self.lock:
            if time.monotonic() - self.loaded_at < max_age:
                return
            enabled, rows = await run_in_threadpool(_read)
            kept: dict[str, SiteLink] = {}
            for row in rows:
                fingerprint = (row["base_url"], bytes(row["token_ciphertext"]), bool(row["enabled"]), row["name"])
                link = self.links.get(row["id"])
                if link is not None and link.fingerprint == fingerprint:
                    kept[row["id"]] = link
                    continue
                try:
                    token = decrypt(row["token_ciphertext"])
                except (RuntimeError, ValueError):
                    logger.warning("The token of site %s cannot be read", row["name"])
                    continue
                fresh = SiteLink(row["id"], row["name"], row["base_url"], token, bool(row["enabled"]), fingerprint, _client(row["base_url"], token), last_contact_at=row["last_contact_at"],
                                 last_checked_at=row["last_checked_at"], last_error=row["last_error"], camera_count=row["camera_count"], open_alarms=row["open_alarms"])
                if link is not None:  # Changed (a new address or token): what was learned about the old one does not carry over, except that it was reached
                    fresh.last_contact_at = link.last_contact_at
                    asyncio.get_running_loop().create_task(link.client.aclose())
                kept[row["id"]] = fresh
            for gone in set(self.links) - set(kept):
                asyncio.get_running_loop().create_task(self.links[gone].client.aclose())
            self.links, self.enabled, self.loaded_at = kept, enabled, time.monotonic()

    def link(self, site_id: str) -> SiteLink | None:
        return self.links.get(site_id)

    def visible(self, access) -> list[SiteLink]:
        return [link for link in self.links.values() if link.enabled and access.can_see_site(link.id)]


def _client(base_url: str, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT, follow_redirects=False, trust_env=False)


registry = Registry()


def _read() -> tuple[bool, list[sqlite3.Row]]:
    with connect() as db:
        return bool(db.execute("SELECT enabled FROM console_settings WHERE singleton_id=1").fetchone()[0]), db.execute("SELECT * FROM console_sites ORDER BY name COLLATE NOCASE").fetchall()


async def active() -> bool:
    await registry.load()
    return registry.enabled


# --- talking to a site ------------------------------------------------------------------------------------------------------------------------

async def call(link: SiteLink, method: str, path: str, *, params: Any = None, body: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    """One request to a site. Raises ``SiteDown`` without trying when the site failed a moment ago."""
    if link.down:
        raise SiteDown(link.last_error or "No answer")
    try:
        response = await link.client.request(method, path, params=params, json=body, headers=headers)
    except httpx.HTTPError as exc:
        link.failed(type(exc).__name__ or "No answer")
        raise SiteDown(link.last_error or "No answer") from exc
    link.answered(refused=response.status_code in (401, 403))
    return response


async def stream(link: SiteLink, method: str, path: str, *, query: str = "", headers: dict[str, str] | None = None, content: bytes | None = None) -> httpx.Response:
    """A request whose answer is read as it arrives (footage, pictures). The caller closes the response."""
    if link.down:
        raise SiteDown(link.last_error or "No answer")
    request = link.client.build_request(method, f"{path}?{query}" if query else path, headers=headers, content=content)
    try:
        response = await link.client.send(request, stream=True)
    except httpx.HTTPError as exc:
        link.failed(type(exc).__name__ or "No answer")
        raise SiteDown(link.last_error or "No answer") from exc
    link.answered(refused=response.status_code in (401, 403))
    return response


async def fan_out(links: list[SiteLink], work: Callable[[SiteLink], Awaitable[Any]]) -> list[tuple[SiteLink, Any]]:
    """Ask every site at once; a site that is down or slow gives an exception in its place instead of delaying the rest."""
    async def one(link: SiteLink) -> Any:
        try:
            return await asyncio.wait_for(work(link), FAN_OUT_SECONDS)
        except asyncio.TimeoutError:
            link.failed("No answer in time")
            return SiteDown("No answer in time")
        except Exception as exc:  # noqa: BLE001 - one site's trouble is its own
            return exc
    return list(zip(links, await asyncio.gather(*(one(link) for link in links))))


# --- rewriting what a site says into the console's terms --------------------------------------------------------------------------------------

_CAMERA_URL = re.compile(r"^(/api/cameras/)([^/?#]+)(/.*)?$")
_TRACK_URL = re.compile(r"^(/api/ai/tracks/)([^/?#]+)(/.*)?$")
_ALARM_URL = re.compile(r"^(/api/alarms/)([^/?#]+)(/.*)?$")


def rewrite_url(site_id: str, value: str) -> str:
    for pattern in (_CAMERA_URL, _TRACK_URL, _ALARM_URL):
        match = pattern.match(value)
        if match:
            return f"{match.group(1)}{prefixed(site_id, match.group(2))}{match.group(3) or ''}"
    return value


def rewrite(site_id: str, value: Any) -> Any:
    """Every URL a site hands out that names one of its cameras, tracks or alarms, and every ``camera_id``, in the console's terms."""
    if isinstance(value, dict):
        return {key: (prefixed(site_id, item) if key == "camera_id" and isinstance(item, str) and item else rewrite(site_id, item)) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite(site_id, item) for item in value]
    if isinstance(value, str) and value.startswith("/api/"):
        return rewrite_url(site_id, value)
    return value


def console_camera(link: SiteLink, camera: dict[str, Any]) -> dict[str, Any]:
    """A site's camera as a console user sees it: named under the site, with nothing about how the site reaches it."""
    view = rewrite(link.id, dict(camera))
    view["id"] = prefixed(link.id, camera["id"])
    view["group_name"] = link.name
    view["site"] = {"id": link.id, "name": link.name}
    for name in HIDDEN_CAMERA_FIELDS:
        if name in view:
            view[name] = None
    view["stream_url"] = ""
    view["storage_volume_id"] = None
    return view


async def inventory(link: SiteLink) -> list[dict[str, Any]]:
    """A site's cameras in the console's terms: fresh when it answers, the last known ones when it does not."""
    if link.cameras_at and time.monotonic() - link.cameras_at < INVENTORY_SECONDS:
        return link.cameras
    try:
        response = await call(link, "GET", "/api/cameras")
    except SiteDown:
        return link.cameras
    if response.status_code == 200:
        link.cameras = [console_camera(link, camera) for camera in response.json().get("cameras", [])]
        link.camera_count = len(link.cameras)
        link.cameras_at = time.monotonic()
    return link.cameras


def site_status(link: SiteLink) -> dict[str, Any]:
    """What a person sees of a site: never its address or token."""
    return {"id": link.id, "name": link.name, "state": link.state, "last_contact_at": link.last_contact_at, "last_error": link.last_error if link.state != "ok" else None,
            "camera_count": link.camera_count, "open_alarms": link.open_alarms}


# --- merging (the hooks the existing list endpoints carry) --------------------------------------------------------------------------------------

@dataclass
class Context:
    kwargs: dict[str, Any]
    user: Any
    run_local: bool = True
    links: list[SiteLink] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


Split = Callable[[dict[str, Any]], Awaitable[tuple[dict[str, Any], Context]]]
Combine = Callable[[Any, Context], Awaitable[Any]]


def federated(split: Split, combine: Combine):
    """Make a list endpoint ask the sites too, when this NVR is a console. Without console mode the endpoint is untouched."""
    def decorate(handler):
        @functools.wraps(handler)
        async def wrapper(*args, **kwargs):
            if not await active():
                return await run_in_threadpool(handler, *args, **kwargs)
            local_kwargs, context = await split(kwargs)
            local = await run_in_threadpool(handler, *args, **local_kwargs) if context.run_local else None
            return await combine(local, context)
        # The handler's annotations may be strings (from __future__ annotations) that only make sense in its own module: resolve them here, so that the framework reads
        # the same parameters from the wrapper that it would from the handler.
        hints = typing.get_type_hints(handler, include_extras=True)
        parameters = [parameter.replace(annotation=hints.get(parameter.name, parameter.annotation)) for parameter in inspect.signature(handler).parameters.values()]
        wrapper.__signature__ = inspect.Signature(parameters, return_annotation=hints.get("return", inspect.Signature.empty))  # type: ignore[attr-defined]
        return wrapper
    return decorate


async def split_plain(kwargs: dict[str, Any]) -> tuple[dict[str, Any], Context]:
    user = kwargs["user"]
    return kwargs, Context(kwargs, user, links=registry.visible(user.access))


async def combine_cameras(local: dict[str, Any], context: Context) -> dict[str, Any]:
    results = await fan_out(context.links, inventory)
    cameras = list(local["cameras"])
    for link, found in results:
        for camera in (found if isinstance(found, list) else link.cameras):
            cameras.append({**camera, "site_unreachable": link.down})
    return {**local, "cameras": cameras, "sites": [site_status(link) for link in context.links]}


async def combine_groups(local: dict[str, Any], context: Context) -> dict[str, Any]:
    """One camera group for each site, so a site's cameras are a group in every view that groups cameras."""
    results = await fan_out(context.links, inventory)
    groups = list(local["groups"])
    for link, found in results:
        ids = [camera["id"] for camera in (found if isinstance(found, list) else link.cameras)]
        groups.append({"id": prefixed(link.id, "all"), "name": link.name, "camera_ids": ids, "camera_count": len(ids), "site": {"id": link.id, "name": link.name}})
    return {**local, "groups": groups}


def _camera_filter(value: str | None) -> tuple[str | None, str | None]:
    """(site, camera id at the site) for a console camera identifier; (None, value) for this NVR's own."""
    if not value:
        return None, value
    parsed = split_id(value)
    return parsed if parsed else (None, value)


async def split_alarms(kwargs: dict[str, Any]) -> tuple[dict[str, Any], Context]:
    user = kwargs["user"]
    context = Context(kwargs, user, links=registry.visible(user.access))
    camera_site, camera = _camera_filter(kwargs.get("camera_id"))
    rule_site, rule = _camera_filter(kwargs.get("rule_id"))
    only = camera_site or rule_site
    if only:
        context.run_local = False
        context.links = [link for link in context.links if link.id == only]
        context.extra = {"camera_id": camera if camera_site else None, "rule_id": rule if rule_site else None}
        if (camera_site and rule_site and camera_site != rule_site) or not context.links:
            context.links = []
    elif kwargs.get("camera_id") or kwargs.get("rule_id"):
        context.links = []  # A filter on this NVR's own camera or rule
    else:
        context.extra = {"camera_id": None, "rule_id": None}
    return kwargs, context


async def combine_alarms(local: dict[str, Any] | None, context: Context) -> dict[str, Any]:
    kwargs = context.kwargs
    params = {"state": kwargs["state"], "limit": kwargs["limit"], **{key: value for key, value in context.extra.items() if value}}

    async def ask(link: SiteLink) -> dict[str, Any] | None:
        response = await call(link, "GET", "/api/alarms", params=params)
        if response.status_code != 200:
            raise SiteDown(f"HTTP {response.status_code}")
        return response.json()

    results = await fan_out(context.links, ask)
    alarms = list(local["alarms"]) if local else []
    counts = dict(local["counts"]) if local else {key: 0 for key in ("new", "acknowledged", "assigned", "resolved")}
    rules = list(local["rules"]) if local else []
    for link, answer in results:
        if not isinstance(answer, dict):
            continue
        for alarm in answer.get("alarms", []):
            view = rewrite(link.id, alarm)
            view["id"] = prefixed(link.id, alarm["id"])
            view["track_id"] = prefixed(link.id, alarm["track_id"]) if alarm.get("track_id") else None
            view["rule_id"] = prefixed(link.id, alarm["rule_id"]) if alarm.get("rule_id") else None
            view["site"] = {"id": link.id, "name": link.name}
            alarms.append(view)
        for key, value in answer.get("counts", {}).items():
            counts[key] = counts.get(key, 0) + int(value)
        rules.extend({"id": prefixed(link.id, rule["id"]), "name": f"{rule['name']} ({link.name})"} for rule in answer.get("rules", []))
    alarms.sort(key=lambda item: (item["occurred_at"], item["id"]), reverse=True)
    return {"alarms": alarms[: kwargs["limit"]], "counts": counts, "rules": rules, "sites": [site_status(link) for link in context.links]}


def _empty_alarms() -> dict[str, Any]:
    return {"alarms": [], "counts": {key: 0 for key in ("new", "acknowledged", "assigned", "resolved")}, "rules": []}


async def split_timeline(kwargs: dict[str, Any]) -> tuple[dict[str, Any], Context]:
    user, payload = kwargs["user"], kwargs["payload"]
    context = Context(kwargs, user)
    local_ids, remote = [], {}
    for camera_id in payload.camera_ids:
        parsed = split_id(camera_id)
        if parsed is None:
            local_ids.append(camera_id)
            continue
        link = registry.link(parsed[0])
        # A camera of a site this person may not see is an unknown camera, as for a hidden local one.
        if link is None or not link.enabled or not user.access.can_see(camera_id):
            raise HTTPException(status_code=422, detail="Playback request contains an unknown camera")
        remote.setdefault(link.id, []).append(parsed[1])
    context.links = [registry.links[site] for site in remote]
    context.extra = {"remote": remote, "order": list(payload.camera_ids)}
    context.run_local = bool(local_ids)
    return {**kwargs, "payload": payload.model_copy(update={"camera_ids": local_ids})}, context


def _unreachable_timeline(link: SiteLink, camera_id: str, start: str, end: str) -> dict[str, Any]:
    named = next((camera for camera in link.cameras if camera["id"] == prefixed(link.id, camera_id)), None)
    return {"camera_id": prefixed(link.id, camera_id), "name": named["name"] if named else camera_id, "recording_status": "site_unreachable", "video_codec": None,
            "audio_supported": None, "audio_enabled": False, "segments": [], "coverage": {}, "motion_events": [], "ai_detections": [], "ai_unanalyzed": [], "external_events": [],
            "gaps": [{"starts_at": start, "ends_at": end, "state": "uncertain", "reason": f"The site {link.name} cannot be reached from the console", "reason_code": "unknown", "backfill": None}]}


async def combine_timeline(local: dict[str, Any] | None, context: Context) -> dict[str, Any]:
    payload = context.kwargs["payload"]
    start, end = payload.start.astimezone(timezone.utc).isoformat(), payload.end.astimezone(timezone.utc).isoformat()
    remote: dict[str, list[str]] = context.extra["remote"]

    async def ask(link: SiteLink) -> list[dict[str, Any]]:
        response = await call(link, "POST", "/api/playback/timeline", body={"camera_ids": remote[link.id], "start": start, "end": end})
        if response.status_code != 200:
            raise SiteDown(f"HTTP {response.status_code}")
        return [{**rewrite(link.id, entry)} for entry in response.json()["cameras"]]

    by_id = {camera["camera_id"]: camera for camera in (local["cameras"] if local else [])}
    for link, answer in await fan_out(context.links, ask):
        for local_id in remote[link.id]:
            by_id[prefixed(link.id, local_id)] = _unreachable_timeline(link, local_id, start, end)
        if isinstance(answer, list):
            by_id.update({entry["camera_id"]: entry for entry in answer})
    return {"start": start, "end": end, "cameras": [by_id[camera_id] for camera_id in context.extra["order"] if camera_id in by_id]}


async def split_search(kwargs: dict[str, Any]) -> tuple[dict[str, Any], Context]:
    user = kwargs["user"]
    wanted = kwargs.get("camera_ids")
    context = Context(kwargs, user)
    if wanted:
        local_ids, remote = [], {}
        for camera_id in wanted:
            parsed = split_id(camera_id)
            if parsed is None:
                local_ids.append(camera_id)
            elif (link := registry.link(parsed[0])) is not None and link.enabled and user.access.can_see(camera_id):
                remote.setdefault(link.id, []).append(parsed[1])
            # A camera that is unknown or hidden is not searched, and does not say so
        context.run_local = bool(local_ids)
        context.links = [registry.links[site] for site in remote]
        context.extra = {"remote": remote}
        return {**kwargs, "camera_ids": local_ids}, context
    context.links = registry.visible(user.access)
    context.extra = {"remote": {link.id: None for link in context.links}}
    return kwargs, context


async def combine_search(local: dict[str, Any] | None, context: Context) -> dict[str, Any]:
    kwargs = context.kwargs
    remote = context.extra["remote"]
    keys = ("start", "end", "label", "upper_colour", "lower_colour", "vehicle_colour", "vehicle_type", "direction", "zone", "order", "limit")
    base = [(key, kwargs[key]) for key in keys if kwargs.get(key) is not None]

    async def ask(link: SiteLink) -> dict[str, Any]:
        params = base + [("camera_ids", camera_id) for camera_id in (remote.get(link.id) or [])]
        response = await call(link, "GET", "/api/ai/search", params=params)
        if response.status_code != 200:
            raise SiteDown(f"HTTP {response.status_code}")
        return response.json()

    answers = await fan_out(context.links, ask)
    results = list(local["results"]) if local else []
    truncated = bool(local and local["truncated"])
    coverage_cameras = list(local["coverage"]["cameras"]) if local else []
    searched = local["coverage"]["searched_cameras"] if local else 0
    percents = [(local["coverage"]["analysed_percent"], local["coverage"]["searched_cameras"])] if local and local["coverage"]["analysed_percent"] is not None else []
    fully = bool(local["coverage"]["fully_analysed"]) if local else True
    unsearched = []
    for link, answer in answers:
        if not isinstance(answer, dict):
            unsearched.append({"site": link.name, "reason": "unreachable"})
            fully = False
            continue
        for item in answer["results"]:
            view = rewrite(link.id, item)
            view["track_id"] = prefixed(link.id, item["track_id"])
            view["site"] = {"id": link.id, "name": link.name}
            results.append(view)
        truncated = truncated or bool(answer["truncated"])
        coverage = answer["coverage"]
        coverage_cameras.extend(rewrite(link.id, coverage["cameras"]))
        searched += coverage["searched_cameras"]
        fully = fully and bool(coverage["fully_analysed"])
        if coverage["analysed_percent"] is not None:
            percents.append((coverage["analysed_percent"], coverage["searched_cameras"]))
    results.sort(key=lambda item: (item["first_seen_at"], item["track_id"]), reverse=kwargs.get("order", "desc") == "desc")
    limit = kwargs["limit"]
    weight = sum(count for _, count in percents)
    base_coverage = local["coverage"] if local else {"range_seconds": 0.0}
    return {"start": (local or {}).get("start") or kwargs["start"], "end": (local or {}).get("end") or kwargs["end"], "results": results[:limit], "truncated": truncated or len(results) > limit,
            "coverage": {**base_coverage, "searched_cameras": searched, "cameras": coverage_cameras, "fully_analysed": fully and not unsearched,
                         "analysed_percent": round(sum(percent * count for percent, count in percents) / weight, 1) if weight else None},
            "coverage_note": (local or {}).get("coverage_note") or "No match in time that was not analysed means nothing was looked at; unanalysed time is listed with the results.",
            "sites_not_searched": unsearched}


# --- the administrator's side -----------------------------------------------------------------------------------------------------------------

async def probe(base_url: str, token: str) -> dict[str, Any]:
    """Check a site and the token before it is kept: it answers as an NVR, the token reads, and it is not an administrator's (the console needs only to read and watch)."""
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT, follow_redirects=False, trust_env=False) as client:
        try:
            cameras = await client.get("/api/cameras")
            if cameras.status_code in (401, 403):
                raise ConsoleError("The site refused the token: create one with the read and media scopes at the site (Settings, API tokens)")
            if cameras.status_code != 200 or "cameras" not in cameras.json():
                raise ConsoleError("That address did not answer like an NVR")
            admin = await client.get("/api/admin/sessions")  # An administrator-only route: refused to a token without the admin scope
            if admin.status_code == 200:
                raise ConsoleError("That token can do far more than the console needs: create one with only the read and media scopes")
            camera_list = cameras.json()["cameras"]
            media = await client.get("/api/insights/reference")  # A route only the media scope opens (it asks for more, which is all that matters here)
            if media.status_code == 403:
                raise ConsoleError("That token cannot watch video: it needs the media scope as well as read")
        except httpx.HTTPError:
            raise ConsoleError("The site could not be reached from here") from None
        except ValueError:
            raise ConsoleError("That address did not answer like an NVR") from None
    return {"camera_count": len(camera_list)}


def new_site_id() -> str:
    return secrets.token_hex(4)


def check_url(url: str, allow_private: bool) -> str:
    try:
        return validate_target_url(url, allow_private).rstrip("/")
    except ValueError as exc:
        raise ConsoleError(str(exc).replace("Webhook", "Site").replace("webhook", "site")) from None


def admin_view(row: sqlite3.Row) -> dict[str, Any]:
    """A site for the administrator: its address, never its token."""
    link = registry.link(row["id"])
    return {"id": row["id"], "name": row["name"], "url": row["base_url"], "allow_private": bool(row["allow_private"]), "enabled": bool(row["enabled"]),
            "state": link.state if link else ("disabled" if not row["enabled"] else "ok"), "last_contact_at": link.last_contact_at if link else row["last_contact_at"],
            "last_checked_at": link.last_checked_at if link else row["last_checked_at"], "last_error": (link.last_error if link else row["last_error"]) if (link.state if link else "ok") != "ok" else None,
            "camera_count": link.camera_count if link else row["camera_count"], "open_alarms": link.open_alarms if link else row["open_alarms"], "created_at": row["created_at"]}


class ConsoleManager:
    """Keeps every site's health current: a camera count and the open alarms, asked of all sites at once, and written down so that the last contact is known after a restart."""

    def __init__(self, seconds: float = HEALTH_SECONDS) -> None:
        self.seconds = seconds
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="console-health")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def check(self) -> None:
        await registry.load(max_age=0)
        if not registry.enabled:
            return

        async def look(link: SiteLink) -> None:
            link.retry_at = 0.0  # The poll is the one request that is allowed to try a site that failed
            cameras = await call(link, "GET", "/api/cameras")
            if cameras.status_code == 200:
                link.cameras = [console_camera(link, camera) for camera in cameras.json().get("cameras", [])]
                link.camera_count, link.cameras_at = len(link.cameras), time.monotonic()
            alarms = await call(link, "GET", "/api/alarms", params={"state": "open", "limit": 1})
            if alarms.status_code == 200:
                link.open_alarms = sum(alarms.json()["counts"].get(key, 0) for key in ("new", "acknowledged", "assigned"))

        await fan_out([link for link in registry.links.values() if link.enabled], look)
        await run_in_threadpool(self._persist)

    def _persist(self) -> None:
        with connect() as db:
            for link in registry.links.values():
                db.execute("UPDATE console_sites SET last_contact_at=?,last_checked_at=?,last_error=?,camera_count=?,open_alarms=? WHERE id=?",
                           (link.last_contact_at, link.last_checked_at, link.last_error, link.camera_count, link.open_alarms, link.id))

    async def _run(self) -> None:
        await asyncio.sleep(2.0)
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("The console's health check failed")
            await asyncio.sleep(self.seconds)
