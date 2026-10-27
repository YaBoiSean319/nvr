"""Camera capability health.

Each thing a camera can do (discovery, authentication, video, audio, motion events, PTZ) has its own state and a plain-language message, so a
camera whose motion events fail still shows working video. Capabilities that failed (or were never checked) are checked again in the background
with a growing, bounded, jittered wait, a few at a time, so a site with many unwell cameras is not hit all at once. Messages come from the NVR's
own fixed phrases and never hold a credential or a credentialed address.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from .credentials import decrypt
from .store import connect

logger = logging.getLogger(__name__)

CAPABILITIES = ("discovery", "authentication", "video", "audio", "motion", "ptz")
STATES = ("ok", "available", "unsupported", "failed", "unknown")
FIRST_RETRY_SECONDS = 30
MAX_RETRY_SECONDS = 1800
UNKNOWN_RETRY_SECONDS = 5
POLL_SECONDS = 15
CHECK_SECONDS = 45  # No single check holds up the others for longer than this
BATCH = 4  # Rechecks started in one poll
RECHECKED = ("video", "motion")  # What the background recheck can test on its own


def now() -> datetime:
    return datetime.now(timezone.utc)


def retry_delay(failures: int) -> float:
    """Seconds to wait after the ``failures``-th failure in a row: 30 s, doubling, never more than 30 minutes, with 20% jitter either way."""
    base = min(MAX_RETRY_SECONDS, FIRST_RETRY_SECONDS * 2 ** max(0, failures - 1))
    return base * random.uniform(0.8, 1.2)


def record(db: sqlite3.Connection, camera_id: str, capability: str, state: str, message: str = "") -> None:
    """Store a capability's state. A failure schedules the next check later each time; a success or an unsupported capability stops the checks."""
    if capability not in CAPABILITIES or state not in STATES:
        raise ValueError("Unknown camera capability or state")
    when = now()
    # The count is kept by the database, so two checks of the same capability at once cannot lose an update.
    db.execute(
        "INSERT INTO camera_capabilities(camera_id,capability,state,message,checked_at,failures,next_retry_at) VALUES(?,?,?,?,?,?,NULL) "
        "ON CONFLICT(camera_id,capability) DO UPDATE SET state=excluded.state,message=excluded.message,checked_at=excluded.checked_at,"
        "failures=CASE WHEN excluded.state='failed' THEN camera_capabilities.failures+1 ELSE 0 END",
        (camera_id, capability, state, message[:300], when.isoformat(), 1 if state == "failed" else 0),
    )
    if state == "failed":
        failures = db.execute("SELECT failures FROM camera_capabilities WHERE camera_id=? AND capability=?", (camera_id, capability)).fetchone()["failures"]
        retry: str | None = (when + timedelta(seconds=retry_delay(failures))).isoformat()
    elif state == "unknown" and capability in RECHECKED:
        retry = (when + timedelta(seconds=UNKNOWN_RETRY_SECONDS)).isoformat()
    else:
        retry = None
    db.execute("UPDATE camera_capabilities SET next_retry_at=? WHERE camera_id=? AND capability=?", (retry, camera_id, capability))


def record_report(db: sqlite3.Connection, camera_id: str, capabilities: dict[str, dict[str, str]]) -> None:
    """Store the capabilities of an inspection report (``camera_inspect.inspect_camera``)."""
    for name, outcome in capabilities.items():
        if name in CAPABILITIES:
            record(db, camera_id, name, outcome.get("state", "unknown"), outcome.get("message", ""))


def view(db: sqlite3.Connection, camera_id: str) -> dict[str, dict[str, Any]]:
    """Every capability of a camera, those never recorded as ``unknown``."""
    rows = {row["capability"]: row for row in db.execute("SELECT * FROM camera_capabilities WHERE camera_id=?", (camera_id,))}
    result: dict[str, dict[str, Any]] = {}
    for name in CAPABILITIES:
        row = rows.get(name)
        result[name] = ({"state": row["state"], "message": row["message"], "checked_at": row["checked_at"], "failures": row["failures"], "next_retry_at": row["next_retry_at"]}
                        if row else {"state": "unknown", "message": "Not checked", "checked_at": None, "failures": 0, "next_retry_at": None})
    return result


def seed(db: sqlite3.Connection, limit: int = 50) -> int:
    """Give cameras saved before capability health existed a video and a motion row, due now, so they are checked once and then kept up to date."""
    rows = db.execute("SELECT id,motion_service_url FROM cameras WHERE enabled=1 AND id NOT IN (SELECT camera_id FROM camera_capabilities WHERE capability='video') LIMIT ?", (limit,)).fetchall()
    for row in rows:
        record(db, row["id"], "video", "unknown", "Not checked yet")
        record(db, row["id"], "motion", "unknown" if row["motion_service_url"] else "unsupported", "Not checked yet" if row["motion_service_url"] else "No ONVIF event service is set")
    return len(rows)


def due(db: sqlite3.Connection, limit: int = BATCH) -> list[tuple[str, str]]:
    """The (camera, capability) pairs whose recheck time has come, oldest first, for cameras that are enabled."""
    marks = ",".join("?" for _ in RECHECKED)
    return [(row["camera_id"], row["capability"]) for row in db.execute(
        f"SELECT k.camera_id,k.capability FROM camera_capabilities k JOIN cameras c ON c.id=k.camera_id WHERE c.enabled=1 AND k.next_retry_at IS NOT NULL "
        f"AND k.next_retry_at<=? AND k.capability IN ({marks}) ORDER BY k.next_retry_at LIMIT ?", (now().isoformat(), *RECHECKED, limit))]


async def check_video(row: sqlite3.Row) -> tuple[str, str]:
    from .onvif_stream import resolve_camera_stream
    from .recording import probe_stream
    stream_url = decrypt(row["stream_url_ciphertext"])
    camera_ip = row["camera_ip"]
    if camera_ip and stream_url == f"rtsp://{camera_ip}/":
        # The camera was saved before ONVIF would give its stream address (a stand-in is stored): ask again.
        credentials = json.loads(decrypt(row["motion_credentials_ciphertext"])) if row["motion_credentials_ciphertext"] else {"username": "", "password": ""}
        try:
            source = await resolve_camera_stream(camera_ip, credentials.get("username", ""), credentials.get("password", ""), row["device_service_url"],
                                                 row["onvif_profile_token"], row["camera_brand"] or "auto")
        except ValueError as error:
            return "failed", str(error)[:300]
        stream_url = source.stream_url
        from .credentials import encrypt
        with connect() as db:
            db.execute("UPDATE cameras SET stream_url_ciphertext=? WHERE id=?", (encrypt(stream_url), row["id"]))
    result = await probe_stream(stream_url)
    if not result.get("reachable"):
        return "failed", str(result.get("reason") or "The camera's video stream could not be opened")[:300]
    video = result.get("video") or {}
    with connect() as db:
        db.execute("UPDATE cameras SET video_codec=?,video_width=?,video_height=?,audio_supported=?,last_probe_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                   (video.get("codec"), video.get("width"), video.get("height"), int(bool(result.get("audio"))), row["id"]))
        record(db, row["id"], "audio", "available" if result.get("audio") else "unsupported", "The camera carries audio" if result.get("audio") else "No audio on the stream")
    return "ok", "Video is flowing" + ("" if row["recording_enabled"] else "; recording is off until you turn it on")


async def check_motion(row: sqlite3.Row) -> tuple[str, str]:
    from .onvif_client import OnvifClient, OnvifError
    from .onvif_events import probe_motion
    url = row["motion_service_url"]
    if not url:
        return "unsupported", "No ONVIF event service is set: the NVR's own motion detection can be used"
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return "failed", "The ONVIF event address is not valid"
    credentials = json.loads(decrypt(row["motion_credentials_ciphertext"])) if row["motion_credentials_ciphertext"] else {"username": "", "password": ""}
    try:
        async with OnvifClient(parsed.hostname or "", credentials.get("username", ""), credentials.get("password", "")) as client:
            device = await client.locate_device(url)
            services = await client.services(device)
            events = services.urls.get("events")
            if not events:
                return "unsupported", "The camera has no ONVIF Events service"
            result = await probe_motion(client, events)
    except OnvifError as error:
        return "failed", error.message
    except ValueError as error:
        return "failed", str(error)[:300]
    return ("ok", "The camera accepted a motion event subscription") if result["ok"] else ("failed", str(result["reason"]))


async def recheck(camera_id: str, capability: str) -> dict[str, Any] | None:
    """Test one capability now and store the answer. ``None`` when the camera is gone or the capability cannot be tested on its own."""
    if capability not in RECHECKED:
        return None
    with connect() as db:
        row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if row is None:
        return None
    try:
        if capability == "video":
            state, message = await asyncio.wait_for(check_video(row), CHECK_SECONDS)
        else:
            state, message = await asyncio.wait_for(check_motion(row), CHECK_SECONDS)
    except Exception:  # noqa: BLE001 - a test that could not run is a failed check, not a crash
        logger.warning("A camera capability could not be checked")
        state, message = "failed", "The check could not be completed"
    try:
        with connect() as db:
            record(db, camera_id, capability, state, message)
            return view(db, camera_id)[capability]
    except sqlite3.IntegrityError:
        return None  # The camera was deleted while it was being checked


class CapabilityManager:
    """Rechecks failed capabilities in the background, in the background worker."""

    def __init__(self, poll_seconds: float = POLL_SECONDS) -> None:
        self.poll_seconds = poll_seconds
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="capabilities")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def process_once(self) -> int:
        with connect() as db:
            seed(db)
            ready = due(db)
        results = await asyncio.gather(*(recheck(camera_id, capability) for camera_id, capability in ready), return_exceptions=True)
        for failed in (item for item in results if isinstance(item, BaseException)):
            logger.warning("A camera capability recheck failed: %s", type(failed).__name__)
        return len(ready)

    async def _run(self) -> None:
        while True:
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - tried again next poll
                logger.exception("The capability recheck failed")
            await asyncio.sleep(self.poll_seconds)
