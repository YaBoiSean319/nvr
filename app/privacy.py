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

from .credentials import decrypt
from .store import connect

MAX_ZONES = 8
GRANT_MINUTES = 15
MASKED_COPY_DIRECTORY = ".masked"
MASKED_COPY_DAYS = 1

_render_locks: dict[str, asyncio.Lock] = {}


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
        since = datetime.fromisoformat(row["camera_masked_since"])
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


def grant_unmasked(user_id: int, camera_id: str, reason: str) -> dict[str, Any]:
    now = _now()
    grant = {"id": str(uuid.uuid4()), "camera_id": camera_id, "reason": reason, "created_at": now.isoformat(),
             "expires_at": (now + timedelta(minutes=GRANT_MINUTES)).isoformat()}
    with connect() as db:
        db.execute("INSERT INTO unmasked_grants(id,user_id,camera_id,reason,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                   (grant["id"], user_id, camera_id, reason, grant["created_at"], grant["expires_at"]))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (user_id, "privacy_unmasked_view_granted", "camera", camera_id,
                    json.dumps({"reason": reason, "expires_at": grant["expires_at"]})))
    return grant


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
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                       (int(principal.id), "privacy_unmasked_viewed", "camera", camera_id,
                        json.dumps({"recorded_at": recorded_at.isoformat() if recorded_at else "live"})))
            return []
        return server_zones(db, camera_id, recorded_at)


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
    """A masked copy of a recording, rendered once per recording and mask and kept for a day."""
    digest = hashlib.sha256(f"{source}|{source.stat().st_mtime_ns}|{json.dumps(zones, sort_keys=True)}".encode()).hexdigest()[:32]
    root = _copies_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{digest}.mp4"
    lock = _render_locks.setdefault(digest, asyncio.Lock())
    async with lock:
        if not target.is_file():
            await asyncio.to_thread(_sweep, root)
            partial = root / f"{digest}.partial.mp4"
            await _ffmpeg(["-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-vf", drawbox_filter(zones),
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "copy", "-movflags", "+faststart", "-y", str(partial)], timeout=600)
            partial.replace(target)
    _render_locks.pop(digest, None)
    return target


async def save(camera_id: str, zones: list[dict[str, float]], actor_id: int) -> dict[str, Any]:
    """Store the masks, and write them to the camera where it accepts ONVIF masks."""
    from .onvif_masks import MasksUnsupported, write_masks

    with connect() as db:
        camera = db.execute("SELECT device_service_url,motion_credentials_ciphertext FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if camera is None:
            raise LookupError("Camera not found")
        previous = mask_row(db, camera_id)
    previous_tokens = json.loads(previous["camera_mask_tokens_json"]) if previous else []
    camera_side, tokens, since = "none", [], None
    if camera["device_service_url"] and (zones or previous_tokens):
        credentials = json.loads(decrypt(camera["motion_credentials_ciphertext"])) if camera["motion_credentials_ciphertext"] else {}
        try:
            tokens = await write_masks(camera["device_service_url"], credentials.get("username", ""), credentials.get("password", ""), zones, previous_tokens)
            camera_side = "applied" if zones else "none"
            since = _now().isoformat() if zones else None
        except MasksUnsupported:
            camera_side = "unsupported"
        except Exception:  # noqa: BLE001 - the NVR masks everything itself until the camera accepts them
            camera_side, tokens = "failed", previous_tokens
    elif zones:
        camera_side = "unsupported"
    with connect() as db:
        db.execute("INSERT INTO privacy_masks(camera_id,zones_json,camera_side,camera_mask_tokens_json,camera_masked_since,updated_at) VALUES(?,?,?,?,?,?) "
                   "ON CONFLICT(camera_id) DO UPDATE SET zones_json=excluded.zones_json,camera_side=excluded.camera_side,"
                   "camera_mask_tokens_json=excluded.camera_mask_tokens_json,camera_masked_since=excluded.camera_masked_since,updated_at=excluded.updated_at",
                   (camera_id, json.dumps(zones), camera_side if zones else "none", json.dumps(tokens), since, _now().isoformat()))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (actor_id, "privacy_masks_updated", "camera", camera_id, json.dumps({"areas": len(zones), "camera_side": camera_side})))
        return status(db, camera_id)
