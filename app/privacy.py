"""Privacy masks: areas of a camera's picture nobody sees, live or recorded.

Where the camera accepts ONVIF privacy masks, they are written to it and footage it
records from then on carries them. Everything else (other cameras, and footage
recorded before the camera accepted its masks) is masked by the NVR on every view:
live video is transcoded with the mask drawn in, recordings and snapshots are
rendered masked, and exports burn it in. An administrator may see unmasked footage
only through a short grant given for a stated reason, which is audited.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .access import may_view_camera
from .credentials import decrypt
from .store import connect

MAX_ZONES = 8
GRANT_MINUTES = 15
MASKED_COPY_DIRECTORY = ".masked"
MASKED_COPY_DAYS = 1
# Footage starting this soon after the camera accepted its masks may still hold earlier frames.
CAMERA_MASK_MARGIN = timedelta(seconds=30)
# Unmasked views of one camera by one person are audited at most this often.
UNMASKED_AUDIT_INTERVAL = timedelta(minutes=1)
MAX_CONCURRENT_RENDERS = 2

_render_locks: dict[str, asyncio.Lock] = {}
_render_slots: asyncio.Semaphore | None = None
_save_locks: dict[str, asyncio.Lock] = {}
# Bumped whenever a camera's masks or grants change: open streams that began under an
# older generation stop, so nobody keeps a picture they would no longer be given.
_generations: dict[str, int] = {}


def generation(camera_id: str) -> int:
    return _generations.get(camera_id, 0)


def _bump(camera_id: str) -> None:
    _generations[camera_id] = generation(camera_id) + 1


class UnmaskedRefused(PermissionError):
    """Unmasked footage was asked for without an active grant."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_zones(zones: list[dict[str, Any]]) -> list[dict[str, float]]:
    if len(zones) > MAX_ZONES:
        raise ValueError(f"Draw up to {MAX_ZONES} masked areas")
    clean = []
    for zone in zones:
        x, y, width, height = (float(zone[key]) for key in ("x", "y", "width", "height"))
        if not (0 <= x < 1 and 0 <= y < 1 and width > 0 and height > 0 and x + width <= 1.0001 and y + height <= 1.0001):
            raise ValueError("Each masked area must lie inside the picture")
        clean.append({"x": round(x, 4), "y": round(y, 4), "width": round(min(width, 1 - x), 4), "height": round(min(height, 1 - y), 4)})
    return clean


def mask_row(db: sqlite3.Connection, camera_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM privacy_masks WHERE camera_id=?", (camera_id,)).fetchone()


def server_zones(db: sqlite3.Connection, camera_id: str, recorded_at: datetime | None = None) -> list[dict[str, float]]:
    """The areas the NVR must draw: for live video (``recorded_at`` None) or footage recorded at that time."""
    row = mask_row(db, camera_id)
    if row is None:
        return []
    zones = json.loads(row["zones_json"])
    if not zones:
        return []
    if row["camera_side"] == "applied" and row["camera_masked_since"]:
        # Trust the camera only while it is the device the masks were written to.
        device = db.execute("SELECT device_service_url FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if device is not None and device["device_service_url"] and device["device_service_url"] == row["written_to"]:
            since = datetime.fromisoformat(row["camera_masked_since"]) + CAMERA_MASK_MARGIN
            if recorded_at is None or recorded_at >= since:
                return []
    return zones


def status(db: sqlite3.Connection, camera_id: str) -> dict[str, Any]:
    row = mask_row(db, camera_id)
    zones = json.loads(row["zones_json"]) if row else []
    camera_side = row["camera_side"] if row else "none"
    since = row["camera_masked_since"] if row else None
    if not zones:
        recordings = "No privacy masks: recordings on disk are not masked."
    elif camera_side == "applied":
        recordings = (f"The camera applies these masks, so recordings on disk from {since} on carry them. "
                      "Older recordings on disk are not masked; the NVR masks them whenever they are viewed or exported.")
    else:
        recordings = ("Recordings on disk are NOT masked: this camera does not apply privacy masks itself. "
                      "The NVR masks every live view, recording, snapshot and export.")
    return {"zones": zones, "camera_side": camera_side, "camera_masked_since": since, "recordings": recordings,
            "server_enforced": bool(server_zones(db, camera_id))}


def drawbox_filter(zones: list[dict[str, float]]) -> str:
    """An FFmpeg filter chain that fills each area black, rounding outwards so no edge pixel escapes."""
    boxes = []
    for zone in zones:
        boxes.append(
            f"drawbox=x='floor(iw*{zone['x']:.4f})':y='floor(ih*{zone['y']:.4f})':"
            f"w='ceil(iw*{zone['width']:.4f})+2':h='ceil(ih*{zone['height']:.4f})+2':color=black:t=fill"
        )
    return ",".join(boxes)


def with_mask(video_filter: str, zones: list[dict[str, float]]) -> str:
    """Prefix a filter chain with the mask, applied before any scaling so it covers the source area."""
    if not zones:
        return video_filter
    return f"{drawbox_filter(zones)},{video_filter}" if video_filter else drawbox_filter(zones)


def _audit(db: sqlite3.Connection, actor_id: int, action: str, camera_id: str, details: dict[str, Any]) -> None:
    db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
               (actor_id, action, "camera", camera_id, json.dumps(details)))


def grant_unmasked(user_id: int, camera_id: str, reason: str) -> dict[str, Any]:
    now = _now()
    grant = {"id": str(uuid.uuid4()), "camera_id": camera_id, "reason": reason, "created_at": now.isoformat(),
             "expires_at": (now + timedelta(minutes=GRANT_MINUTES)).isoformat()}
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO unmasked_grants(id,user_id,camera_id,reason,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                       (grant["id"], user_id, camera_id, reason, grant["created_at"], grant["expires_at"]))
            _audit(db, user_id, "privacy_unmasked_view_granted", camera_id, {"reason": reason, "expires_at": grant["expires_at"]})
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return grant


def revoke_unmasked(user_id: int, camera_id: str | None = None) -> None:
    """End this person's grants (for one camera, or all on sign-out); unmasked streams they hold stop."""
    with connect() as db:
        rows = db.execute("SELECT DISTINCT camera_id FROM unmasked_grants WHERE user_id=? AND (? IS NULL OR camera_id=?) AND expires_at>?",
                          (user_id, camera_id, camera_id, _now().isoformat())).fetchall()
        db.execute("DELETE FROM unmasked_grants WHERE user_id=? AND (? IS NULL OR camera_id=?)", (user_id, camera_id, camera_id))
        for row in rows:
            _audit(db, user_id, "privacy_unmasked_view_ended", row["camera_id"], {})
            _bump(row["camera_id"])


def active_grant(db: sqlite3.Connection, user_id: int, camera_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM unmasked_grants WHERE user_id=? AND camera_id=? AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
                      (user_id, camera_id, _now().isoformat())).fetchone()


def zones_for(principal: Any, camera_id: str, *, unmasked: bool = False, recorded_at: datetime | None = None) -> list[dict[str, float]]:
    """The areas to mask in media for this caller. Asking for unmasked media needs an administrator's
    own session (never an API token) with an active grant for this camera; anything else is refused."""
    with connect() as db:
        if unmasked:
            if principal.role != "administrator" or principal.token_id is not None or active_grant(db, int(principal.id), camera_id) is None:
                raise UnmaskedRefused("Unmasked footage needs an administrator's stated reason first")
            recent = db.execute("SELECT 1 FROM audit_log WHERE actor_id=? AND action='privacy_unmasked_viewed' AND resource_id=? AND created_at>?",
                                (int(principal.id), camera_id, (_now() - UNMASKED_AUDIT_INTERVAL).strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
            if recent is None:
                _audit(db, int(principal.id), "privacy_unmasked_viewed", camera_id, {"recorded_at": recorded_at.isoformat() if recorded_at else "live"})
            return []
        return server_zones(db, camera_id, recorded_at)


ACCESS_RECHECK_SECONDS = 1.0


class StreamGuard:
    """For an open live stream: stops it when the camera's masks or grants change, its grant lapses, or the
    viewer's access to the camera ends (re-checked every second, so an expired access grant cuts a stream at once)."""

    def __init__(self, principal: Any, camera_id: str, unmasked: bool) -> None:
        self.camera_id = camera_id
        self.user_id = int(principal.id)
        self.reason: str | None = None  # "access" or "masks", once it has stopped being valid
        self._access_checked = time.monotonic()
        self.generation = generation(camera_id)
        self.until: datetime | None = None
        if unmasked:
            with connect() as db:
                grant = active_grant(db, int(principal.id), camera_id)
            self.until = datetime.fromisoformat(grant["expires_at"]) if grant else _now()

    def still_valid(self) -> bool:
        if self.reason is not None:
            return False
        if time.monotonic() - self._access_checked >= ACCESS_RECHECK_SECONDS:
            self._access_checked = time.monotonic()
            with connect() as db:
                if not may_view_camera(db, self.user_id, self.camera_id):
                    self.reason = "access"
                    return False
        if generation(self.camera_id) != self.generation or (self.until is not None and _now() >= self.until):
            self.reason = "masks"
            return False
        return True


async def _ffmpeg(args: list[str], stdin: bytes | None = None, timeout: float = 120) -> bytes:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("FFmpeg is unavailable")
    process = await asyncio.create_subprocess_exec(executable, "-nostdin", "-hide_banner", "-loglevel", "error", *args,
                                                   stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        raise RuntimeError("FFmpeg could not apply the privacy mask")
    return output


async def mask_jpeg(jpeg: bytes, zones: list[dict[str, float]]) -> bytes:
    if not zones:
        return jpeg
    return await _ffmpeg(["-f", "image2pipe", "-i", "pipe:0", "-vf", drawbox_filter(zones), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"], jpeg, 20)


def mask_jpeg_sync(jpeg: bytes, zones: list[dict[str, float]]) -> bytes | None:
    """``mask_jpeg`` for synchronous callers. None when the mask could not be drawn: never the unmasked picture."""
    import subprocess

    if not zones:
        return jpeg
    executable = shutil.which("ffmpeg")
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "image2pipe", "-i", "pipe:0",
                                 "-vf", drawbox_filter(zones), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                                input=jpeg, capture_output=True, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        return None
    return result.stdout if result.returncode == 0 and result.stdout.startswith(b"\xff\xd8") else None


def _copies_root() -> Path:
    from .storage import recordings_path

    return recordings_path() / MASKED_COPY_DIRECTORY


def _sweep(root: Path) -> None:
    cutoff = time.time() - MASKED_COPY_DAYS * 86400
    for item in root.glob("*.mp4"):
        try:
            if item.stat().st_mtime < cutoff:
                item.unlink()
        except OSError:
            pass


async def masked_recording(source: Path, zones: list[dict[str, float]]) -> Path:
    """A masked copy of a recording, rendered once per recording and mask and kept for a day.
    Renders are few at a time, so scrubbing through masked footage cannot exhaust the processor."""
    global _render_slots
    digest = hashlib.sha256(f"{source}|{source.stat().st_mtime_ns}|{json.dumps(zones, sort_keys=True)}".encode()).hexdigest()[:32]
    root = _copies_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{digest}.mp4"
    lock = _render_locks.setdefault(digest, asyncio.Lock())
    try:
        async with lock:
            if target.is_file():
                target.touch()  # In use: not swept for another day.
                return target
            if _render_slots is None:
                _render_slots = asyncio.Semaphore(MAX_CONCURRENT_RENDERS)
            async with _render_slots:
                await asyncio.to_thread(_sweep, root)
                partial = root / f"{digest}.{uuid.uuid4().hex}.partial.mp4"
                try:
                    await _ffmpeg(["-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-vf", drawbox_filter(zones),
                                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "copy", "-movflags", "+faststart", "-y", str(partial)], timeout=600)
                    partial.replace(target)
                finally:
                    partial.unlink(missing_ok=True)
            return target
    finally:
        if not lock.locked():
            _render_locks.pop(digest, None)


async def save(camera_id: str, zones: list[dict[str, float]], actor_id: int) -> dict[str, Any]:
    """Store the masks, and write them to the camera where it accepts ONVIF masks."""
    lock = _save_locks.setdefault(camera_id, asyncio.Lock())
    async with lock:
        return await _save(camera_id, zones, actor_id)


async def _save(camera_id: str, zones: list[dict[str, float]], actor_id: int) -> dict[str, Any]:
    from .onvif_masks import MasksUnsupported, MaskWriteFailed, write_masks

    with connect() as db:
        camera = db.execute("SELECT device_service_url,motion_credentials_ciphertext FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if camera is None:
            raise LookupError("Camera not found")
        previous = mask_row(db, camera_id)
    previous_tokens = json.loads(previous["camera_mask_tokens_json"]) if previous else []
    camera_side, tokens, since, written_to = "none", previous_tokens, None, None
    if camera["device_service_url"] and (zones or previous_tokens):
        credentials = json.loads(decrypt(camera["motion_credentials_ciphertext"])) if camera["motion_credentials_ciphertext"] else {}
        try:
            tokens = await write_masks(camera["device_service_url"], credentials.get("username", ""), credentials.get("password", ""), zones, previous_tokens)
            camera_side = "applied" if zones else "none"
            since = _now().isoformat() if zones else None
            written_to = camera["device_service_url"]
        except MasksUnsupported:
            camera_side = "unsupported"  # Nothing was changed on the camera.
        except MaskWriteFailed as exc:
            # The old masks are gone and these were created: remember them to remove next time.
            camera_side, tokens = "failed", exc.created
        except Exception:  # noqa: BLE001 - the NVR masks everything itself until the camera accepts them
            camera_side = "failed"
    elif zones:
        camera_side = "unsupported"
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO privacy_masks(camera_id,zones_json,camera_side,camera_mask_tokens_json,camera_masked_since,written_to,updated_at) VALUES(?,?,?,?,?,?,?) "
                       "ON CONFLICT(camera_id) DO UPDATE SET zones_json=excluded.zones_json,camera_side=excluded.camera_side,"
                       "camera_mask_tokens_json=excluded.camera_mask_tokens_json,camera_masked_since=excluded.camera_masked_since,"
                       "written_to=excluded.written_to,updated_at=excluded.updated_at",
                       (camera_id, json.dumps(zones), camera_side if zones else "none", json.dumps(tokens), since, written_to, _now().isoformat()))
            _audit(db, actor_id, "privacy_masks_updated", camera_id, {"areas": len(zones), "camera_side": camera_side})
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        _bump(camera_id)
        return status(db, camera_id)
