"""Live video kept ready (#216).

The first person to open a camera made the NVR connect to it and wait for it to start sending: 2.5 to 6 seconds on the owner's cameras, nearly all of it the camera
getting going. The rest of the NVR already shares one connection among everyone watching a camera, so the way to take the wait away is to have that connection open
before anyone asks. Three modes, set in Settings:

* ``all`` (the default): the keeper holds the live stream of every camera open all the time, the one a viewer opening a grid would be given (the substream if
  the camera has one, else the main stream), prepared exactly as for a viewer, so the first viewer joins a stream that is already running.
* ``recent``: nothing is held ahead, but a stream stays open ten minutes after its last viewer leaves instead of five seconds.
* ``off``: as before.

A held stream is one more connection to the camera (the recorder holds another), and with a privacy mask drawn in it is re-encoded; the keeper holds at most
``MAX_READY_STREAMS`` of them and skips streams that would be converted from H.265 (which only some browsers need). A camera that does not answer is retried with
growing gaps, not every pass.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any

from . import live_variant, privacy
from .credentials import decrypt
from .live_hub import LiveHub, LiveStream, SinkViewer
from .recording import safe_stream_url
from .store import connect

logger = logging.getLogger("nvr.live.ready")

MODES = ("off", "recent", "all")
DEFAULT_MODE = "all"
RECENT_LINGER_SECONDS = 600.0
MAX_READY_STREAMS = 24
CHECK_SECONDS = 15.0
RETRY_FIRST_SECONDS, RETRY_MAX_SECONDS = 15.0, 300.0


def get_mode(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT mode FROM live_ready_settings WHERE singleton_id=1").fetchone()
    return row["mode"] if row and row["mode"] in MODES else DEFAULT_MODE


def set_mode(db: sqlite3.Connection, mode: str) -> None:
    db.execute("UPDATE live_ready_settings SET mode=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE singleton_id=1", (mode,))


def wanted(db: sqlite3.Connection) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    """The streams to hold ready: (key, how to start it) for each camera, in a fixed order, at most MAX_READY_STREAMS.

    Each is the stream a viewer opening a grid gets (``quality=low``, a browser that does not play H.265), made the way the live endpoint makes it."""
    result = []
    for camera in db.execute("SELECT id,stream_url_ciphertext,live_substream_url_ciphertext,video_codec,audio_enabled FROM cameras "
                             "WHERE archived_at IS NULL AND enabled=1 ORDER BY name,id"):
        try:
            variant = live_variant.choose(camera, privacy.server_zones(db, camera["id"]), "low", False)
            if variant.transcode or not variant.playable_directly:
                continue  # Converting H.265 for every viewer is real work; a camera the browser cannot play directly has no stream to hold
            source = decrypt(camera["live_substream_url_ciphertext"] if variant.profile == "sub" else camera["stream_url_ciphertext"])
            safe_stream_url(source)
        except Exception:  # noqa: BLE001 - one camera with a bad address must not stop the others being held
            logger.warning("Live video for camera %s cannot be held ready: its stream address is unusable", camera["id"])
            continue
        result.append((live_variant.key_of(camera["id"], variant, source),
                       {"source": source, "include_audio": variant.include_audio, "video_filter": variant.video_filter,
                        "transcode": variant.transcode, "hevc_copy": variant.hevc_copy}))
        if len(result) >= MAX_READY_STREAMS:
            break
    return result


class LiveReadyKeeper:
    """Holds the wanted streams open in the live hub, and sets how long unwatched streams linger."""

    def __init__(self, hub: LiveHub, check_seconds: float = CHECK_SECONDS) -> None:
        self.hub = hub
        self.check_seconds = check_seconds
        self.task: asyncio.Task[None] | None = None
        self.held: dict[tuple[str, ...], tuple[LiveStream, SinkViewer]] = {}
        self._retry: dict[tuple[str, ...], tuple[float, float]] = {}  # key -> (not before, next gap)
        self._wake = asyncio.Event()

    async def start(self) -> None:
        self._wake = asyncio.Event()
        self.task = asyncio.create_task(self._run(), name="live-ready")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        self.held.clear()  # The hub closes its streams itself

    def nudge(self) -> None:
        """Look again now (the mode was changed in Settings)."""
        self._wake.set()

    def status(self) -> list[dict[str, object]]:
        return [{"camera_id": key[0], "profile": key[1], "state": "failed" if stream.failed else "ready" if stream.ready.is_set() else "starting"}
                for key, (stream, _) in self.held.items()]

    def apply(self, mode: str, desired: list[tuple[tuple[str, ...], dict[str, Any]]], now: float | None = None) -> None:
        """Bring the held streams to what the mode and the cameras ask for."""
        now = time.monotonic() if now is None else now
        self.hub.linger_seconds = RECENT_LINGER_SECONDS if mode != "off" else None
        for stream in self.hub.streams.values():
            stream.linger_seconds = self.hub.linger_seconds
        wanted_keys = {key for key, _ in desired} if mode == "all" else set()
        for key in [key for key in self.held if key not in wanted_keys]:
            stream, viewer = self.held.pop(key)
            self.hub.leave(stream, viewer)
            self._retry.pop(key, None)
        for key, how in desired if mode == "all" else []:
            held = self.held.get(key)
            live = self.hub.streams.get(key)
            if held and held[0] is live and not live.failed:
                if live.ready.is_set():
                    self._retry.pop(key, None)
                continue
            if held:
                # It failed (or a viewer's join replaced it): let it go and wait before trying again, longer each time.
                self.hub.leave(*held)
                del self.held[key]
                gap = self._retry.get(key, (0.0, RETRY_FIRST_SECONDS))[1]
                self._retry[key] = (now + gap, min(gap * 2, RETRY_MAX_SECONDS))
                continue
            if now < self._retry.get(key, (0.0, RETRY_FIRST_SECONDS))[0]:
                continue
            from .api.live_media import mark_camera_fresh
            viewer = SinkViewer()
            # The same freshness mark a viewer's own join would have set up: the stream keeps the first one it was given, which is this one.
            stream = self.hub.join(key, how["source"], how["include_audio"], viewer, lambda camera_id=key[0]: mark_camera_fresh(camera_id),
                                   how["video_filter"], how["transcode"], how["hevc_copy"])
            self.held[key] = (stream, viewer)

    def _load(self) -> tuple[str, list[tuple[tuple[str, ...], dict[str, Any]]]]:
        with connect() as db:
            mode = get_mode(db)
            return mode, (wanted(db) if mode == "all" else [])

    async def _run(self) -> None:
        while True:
            try:
                mode, desired = await asyncio.to_thread(self._load)
                self.apply(mode, desired)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep looking after, and holding, what was already held
                logger.exception("Keeping live video ready failed; trying again")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.check_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
