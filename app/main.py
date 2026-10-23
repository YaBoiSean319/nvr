from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, field_validator, model_validator

from .application import APP_VERSION, create_app
from .api.ai_settings import _ai_runtime_model_view, _ensure_ai_settings
from .api.cameras import _camera_view
from .viewing import MAX_LIVE_AUDIO_LISTENERS, MAX_LIVE_VIEWERS
from .principal import Administrator, CurrentUser, administrator, current_user  # noqa: F401 - dependencies are overridden through this module
from .store import connect, database_path
from .credentials import decrypt
from .recording import safe_stream_url, _manifest_bytes
from .health import recording_health
from .scheduling import effective_policy
from . import diagnostics
from .retention import acquire_playback, release_playback
from .preservation import delete_segment as delete_preserved_segment, get_segment_hold, preserve_segment, release_segment_hold
from .exports import create_export_record, export_output_path, get_export, list_exports, request_cancel, verify_evidence_bytes, _job_view, MAX_EXPORT_CAMERAS, MAX_EXPORT_WINDOW, MAX_VERIFY_PACKAGE_BYTES
from .incidents import acknowledge_incident, incident_view, list_incidents
from .webhooks import EVENT_TYPES as WEBHOOK_EVENT_TYPES, create_endpoint as create_webhook_endpoint, delete_endpoint as delete_webhook_endpoint, enqueue_test as enqueue_webhook_test, list_deliveries as list_webhook_deliveries, list_endpoints as list_webhook_endpoints, rotate_secret as rotate_webhook_secret, update_endpoint as update_webhook_endpoint
from .heartbeat import configure_heartbeat, disable_heartbeat, heartbeat_status, queue_heartbeat_test, rotate_heartbeat_secret

_live_view_lock = threading.Lock()
_live_view_count = 0
_live_audio_count = 0
_live_camera_status: dict[str, dict[str, object]] = {}


app = create_app()


class SynchronizedPlaybackRequest(BaseModel):
    camera_ids: list[str] = Field(min_length=1, max_length=16)
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_interval(self) -> "SynchronizedPlaybackRequest":
        if len(set(self.camera_ids)) != len(self.camera_ids):
            raise ValueError("Choose each camera only once")
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError("Playback interval must include timezone-aware start and end times")
        if self.end - self.start > timedelta(hours=24):
            raise ValueError("Synchronized playback is limited to 24 hours per search")
        return self


class ExportRequest(BaseModel):
    camera_ids: list[str] = Field(min_length=1, max_length=MAX_EXPORT_CAMERAS)
    start: datetime
    end: datetime
    output_modes: list[Literal["individual", "grid"]] = Field(default_factory=lambda: ["individual", "grid"], min_length=1, max_length=2)
    tile_camera_ids: list[str | None] = Field(default_factory=list, max_length=16)
    grid_resolution: Literal["1080p", "4k"] = "1080p"
    include_audio: bool = False
    audio_camera_id: str | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "ExportRequest":
        if len(set(self.camera_ids)) != len(self.camera_ids):
            raise ValueError("Choose each camera only once")
        if len(set(self.output_modes)) != len(self.output_modes):
            raise ValueError("Choose each export output only once")
        if self.include_audio and self.audio_camera_id is not None and self.audio_camera_id not in self.camera_ids:
            raise ValueError("Audio source must be one of the selected cameras")
        if self.audio_camera_id is not None and (not self.include_audio or "grid" not in self.output_modes):
            raise ValueError("A grid audio source requires audio and a synchronized grid output")
        if self.include_audio and "grid" in self.output_modes and not self.audio_camera_id:
            raise ValueError("Choose one selected camera as the grid audio source")
        if self.tile_camera_ids and (len(self.tile_camera_ids) not in {1, 4, 9, 16} or
                len([item for item in self.tile_camera_ids if item is not None]) != len(set(item for item in self.tile_camera_ids if item is not None)) or
                set(item for item in self.tile_camera_ids if item is not None) != set(self.camera_ids)):
            raise ValueError("Grid tiles must include each selected camera exactly once")
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError("Export interval must include timezone-aware start and end times")
        if self.end - self.start > MAX_EXPORT_WINDOW:
            raise ValueError("Exports are limited to 24 hours per job")
        return self


class WebhookCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=2048)
    events: list[str] = Field(min_length=1, max_length=8)
    allow_private_receiver: bool = False

    @field_validator("events")
    @classmethod
    def validate_events(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(event not in WEBHOOK_EVENT_TYPES - {"webhook.test"} for event in value):
            raise ValueError("Select one or more supported incident event types")
        return value


class WebhookUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=8, max_length=2048)
    events: list[str] | None = Field(default=None, min_length=1, max_length=8)
    allow_private_receiver: bool | None = None
    enabled: bool | None = None

    @field_validator("events")
    @classmethod
    def validate_events(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (len(set(value)) != len(value) or any(event not in WEBHOOK_EVENT_TYPES - {"webhook.test"} for event in value)):
            raise ValueError("Select one or more supported incident event types")
        return value


class HeartbeatConfigRequest(BaseModel):
    target_url: str = Field(min_length=8, max_length=2048)
    interval_seconds: int = Field(default=60, ge=30, le=900)
    allow_private_receiver: bool = False


class CameraFootageDeleteRequest(BaseModel):
    confirm_camera_name: str = Field(min_length=1, max_length=120)

    @field_validator("confirm_camera_name")
    @classmethod
    def trim_confirmed_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter the archived camera name to confirm deletion")
        return value


class HoldRequest(BaseModel):
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class DiagnosticCaptureRequest(BaseModel):
    enabled: bool
    hours: int = Field(default=4, ge=1, le=24)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(Path(__file__).with_name("static") / "index.html")


STATIC_JAVASCRIPT_ASSETS = frozenset({
    "app.js",
    "workspace-layout.js",
    "camera-inventory.js",
    "camera-editor.js",
    "camera-admin-save.js",
    "camera-discovery.js",
    "camera-onboarding.js",
    "camera-preview.js",
    "camera-preview-ui.js",
    "camera-bulk-actions.js",
    "camera-groups.js",
})
STATIC_STYLESHEET_ASSETS = frozenset({"camera-groups.css"})


@app.get("/{asset_name}.js", include_in_schema=False)
def javascript_asset(asset_name: str) -> FileResponse:
    filename = f"{asset_name}.js"
    if filename not in STATIC_JAVASCRIPT_ASSETS:
        raise HTTPException(status_code=404, detail="JavaScript asset not found")
    path = Path(__file__).with_name("static") / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="JavaScript asset not found")
    return FileResponse(path, media_type="text/javascript")


@app.get("/{asset_name}.css", include_in_schema=False)
def stylesheet_asset(asset_name: str) -> FileResponse:
    filename = f"{asset_name}.css"
    if filename not in STATIC_STYLESHEET_ASSETS:
        raise HTTPException(status_code=404, detail="Stylesheet asset not found")
    path = Path(__file__).with_name("static") / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Stylesheet asset not found")
    return FileResponse(path, media_type="text/css")


def _acquire_live_view(camera_id: str) -> bool:
    global _live_view_count
    with _live_view_lock:
        if _live_view_count >= MAX_LIVE_VIEWERS:
            return False
        _live_view_count += 1
        status_data = _live_camera_status.setdefault(camera_id, {"viewers": 0, "last_frame_at": None})
        status_data["viewers"] = int(status_data["viewers"]) + 1
        return True


def _release_live_view(camera_id: str) -> None:
    global _live_view_count
    with _live_view_lock:
        _live_view_count = max(0, _live_view_count - 1)
        status_data = _live_camera_status.get(camera_id)
        if status_data:
            status_data["viewers"] = max(0, int(status_data["viewers"]) - 1)
            if not status_data["viewers"]:
                _live_camera_status.pop(camera_id, None)


def _acquire_live_audio() -> bool:
    global _live_audio_count
    with _live_view_lock:
        if _live_audio_count >= MAX_LIVE_AUDIO_LISTENERS:
            return False
        _live_audio_count += 1
        return True


def _release_live_audio() -> None:
    global _live_audio_count
    with _live_view_lock:
        _live_audio_count = max(0, _live_audio_count - 1)


@app.get("/api/cameras/{camera_id}/live/status")
def live_view_status(camera_id: str, _: CurrentUser) -> dict[str, object]:
    with connect() as db:
        camera = db.execute("SELECT archived_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if camera is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if camera["archived_at"]:
            raise HTTPException(status_code=410, detail="Camera is archived")
    with _live_view_lock:
        active = _live_camera_status.get(camera_id)
        count = int(active["viewers"]) if active else 0
        last_frame_at = active["last_frame_at"] if active else None
    fresh = False
    if last_frame_at:
        try:
            fresh = (datetime.now(timezone.utc) - datetime.fromisoformat(str(last_frame_at))).total_seconds() <= 12
        except ValueError:
            fresh = False
    state = "live" if fresh else "stale" if count else "not_viewing"
    return {"camera_id": camera_id, "active_viewers": count, "last_frame_at": last_frame_at, "state": state}


@app.get("/api/cameras/{camera_id}/audio/live")
def live_camera_audio(
    camera_id: str,
    _: CurrentUser,
) -> StreamingResponse:
    with connect() as db:
        camera = db.execute("SELECT stream_url_ciphertext,audio_supported,audio_enabled,archived_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    if camera["archived_at"]:
        raise HTTPException(status_code=410, detail="Camera is archived")
    if not camera["audio_enabled"]:
        raise HTTPException(status_code=403, detail="Camera audio listening is disabled by an administrator")
    if not camera["audio_supported"]:
        raise HTTPException(status_code=409, detail="The camera stream does not currently provide a supported audio track")
    executable = shutil.which("ffmpeg")
    if not executable:
        raise HTTPException(status_code=503, detail="Live audio transcoding is unavailable")
    if not _acquire_live_audio():
        raise HTTPException(status_code=503, detail="The live-audio listener is already in use; close it before listening to another camera")
    try:
        source = decrypt(camera["stream_url_ciphertext"])
        safe_stream_url(source)
    except (RuntimeError, ValueError) as exc:
        _release_live_audio()
        raise HTTPException(status_code=503, detail="Camera stream configuration is unavailable") from exc

    async def stream():
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                executable, "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                "-protocol_whitelist", "concat,pipe,rtsp,rtsps,tcp,udp,rtp,tls,crypto", "-i", "pipe:0",
                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "44100", "-b:a", "96k",
                "-c:a", "libmp3lame", "-f", "mp3", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            if process.stdin is None or process.stdout is None:
                return
            process.stdin.write(_manifest_bytes(source))
            await process.stdin.drain()
            process.stdin.close()
            while chunk := await process.stdout.read(32 * 1024):
                yield chunk
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError):
            return
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            _release_live_audio()

    return StreamingResponse(stream(), media_type="audio/mpeg", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def _mjpeg_frames(camera_id: str, source: str, quality: str):
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("Live video transcoding is unavailable")
    width, fps, jpeg_quality = (640, 6, 8) if quality == "low" else (1280, 12, 5)
    args = [
        executable, "-hide_banner", "-nostats", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-protocol_whitelist", "concat,pipe,rtsp,rtsps,tcp,udp,rtp,tls,crypto", "-i", "pipe:0",
        "-map", "0:v:0", "-an", "-vf", f"fps={fps},scale={width}:-2:force_original_aspect_ratio=decrease",
        "-q:v", str(jpeg_quality), "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
    ]
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    if process.stdin is None or process.stdout is None:
        raise RuntimeError("Live video transcoder did not start")
    from .recording import _manifest_bytes
    process.stdin.write(_manifest_bytes(source))
    await process.stdin.drain()
    process.stdin.close()
    buffer = bytearray()
    try:
        while True:
            chunk = await asyncio.wait_for(process.stdout.read(16384), timeout=15)
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 1:
                        del buffer[:-1]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start:
                        del buffer[:start]
                    if len(buffer) > 4 * 1024 * 1024:
                        raise RuntimeError("Camera frame exceeded the live-view limit")
                    break
                frame = bytes(buffer[start:end + 2])
                del buffer[:end + 2]
                with _live_view_lock:
                    status_data = _live_camera_status.get(camera_id)
                    if status_data:
                        status_data["last_frame_at"] = datetime.now(timezone.utc).isoformat()
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


@app.get("/api/cameras/{camera_id}/live")
def live_camera(
    camera_id: str,
    _: CurrentUser,
    quality: Literal["low", "high"] = Query(default="low"),
) -> StreamingResponse:
    with connect() as db:
        camera = db.execute("SELECT stream_url_ciphertext,live_substream_url_ciphertext,archived_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    if camera["archived_at"]:
        raise HTTPException(status_code=410, detail="Camera is archived")
    if not shutil.which("ffmpeg"):
        raise HTTPException(status_code=503, detail="Live video transcoding is unavailable")
    if not _acquire_live_view(camera_id):
        raise HTTPException(status_code=503, detail="All live-view slots are in use; close another view and retry")
    source = decrypt(camera["live_substream_url_ciphertext"] or camera["stream_url_ciphertext"]) if quality == "low" else decrypt(camera["stream_url_ciphertext"])
    try:
        safe_stream_url(source)
    except ValueError as exc:
        _release_live_view(camera_id)
        raise HTTPException(status_code=503, detail="Camera stream configuration is invalid") from exc

    async def stream():
        try:
            async for frame in _mjpeg_frames(camera_id, source, quality):
                yield frame
        except (OSError, RuntimeError, asyncio.TimeoutError):
            return
        finally:
            _release_live_view(camera_id)

    return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/system/status")
def system_status(user: Administrator) -> dict[str, object]:
    db_path = database_path()
    with connect() as db:
        admin_count = db.execute("SELECT COUNT(*) FROM users WHERE role='administrator'").fetchone()[0]
        camera_count = db.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
    try:
        disk = os.statvfs(db_path.parent)
        storage = {"available_bytes": disk.f_bavail * disk.f_frsize, "total_bytes": disk.f_blocks * disk.f_frsize}
    except OSError:
        storage = {"available_bytes": None, "total_bytes": None}
    return {
        "application": "Workplace NVR",
        "version": APP_VERSION,
        "application_status": "healthy",
        "database": "connected",
        "configured_administrators": admin_count,
        "configured_cameras": camera_count,
        "storage": storage,
    }


@app.get("/api/diagnostics/status")
def diagnostics_status(_: Administrator) -> dict[str, object]:
    return diagnostics.capture_status()


@app.post("/api/diagnostics/capture")
def diagnostics_capture(
    payload: DiagnosticCaptureRequest,
    _: Administrator,
) -> dict[str, object]:
    return diagnostics.set_capture(payload.enabled, payload.hours)


@app.get("/api/diagnostics/bundle")
def diagnostics_bundle(_: Administrator) -> Response:
    return Response(
        content=json.dumps(diagnostics.bundle(), indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="nvr-diagnostics.json"', "Cache-Control": "no-store"},
    )


@app.get("/api/cameras/{camera_id}/motion-events")
def list_camera_motion_events(
    camera_id: str,
    _: CurrentUser,
) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        rows = db.execute(
            "SELECT id,source,source_topic,source_event_at,received_at,requested_start,requested_end,actual_start,actual_end,pre_roll_complete,status,failure_reason,segment_id "
            "FROM motion_events WHERE camera_id=? ORDER BY received_at DESC LIMIT 200", (camera_id,)
        ).fetchall()
    events = []
    for row in rows:
        item = dict(row)
        item["pre_roll_complete"] = bool(item["pre_roll_complete"])
        item["media_url"] = f"/api/cameras/{camera_id}/segments/{item['segment_id']}/media" if item["segment_id"] else None
        events.append(item)
    return {"events": events}


@app.get("/api/ai/events")
def search_ai_observations(
    _: CurrentUser,
    camera_id: str | None = Query(default=None, max_length=64),
    label: Literal["person", "vehicle"] | None = None,
    start: str | None = Query(default=None, max_length=64),
    end: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    clauses: list[str] = []
    values: list[object] = []
    if start and end:
        try:
            start_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_time = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if start_time.tzinfo is None or end_time.tzinfo is None or end_time <= start_time:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Search range must be timezone-aware and have a positive duration") from exc
    if camera_id:
        with connect() as db:
            if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="Camera not found")
        clauses.append("o.camera_id=?")
        values.append(camera_id)
    if label:
        clauses.append("o.label=?")
        values.append(label)
    for column, raw, operator in (("o.first_seen_at", start, ">="), ("o.first_seen_at", end, "<=")):
        if raw:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
                values.append(parsed.astimezone(timezone.utc).isoformat())
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Search timestamps must include a timezone") from exc
            clauses.append(f"{column}{operator}?")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    values.append(limit)
    with connect() as db:
        rows = db.execute(
            "SELECT o.*,c.name AS camera_name FROM ai_observations o JOIN cameras c ON c.id=o.camera_id" + where + " ORDER BY o.first_seen_at DESC LIMIT ?",
            values,
        ).fetchall()
    events = []
    for row in rows:
        item = dict(row)
        item["box"] = json.loads(item.pop("box_json"))
        events.append(item)
    models_used = sorted({(str(item["model_id"]), str(item["model_version"])) for item in events})
    return {"events": events, "model": _ai_runtime_model_view(), "models_used": [{"id": model_id, "version": version} for model_id, version in models_used], "coverage_note": "Search results describe analyzed frames only; empty results do not prove the scene was empty."}


@app.get("/api/cameras/{camera_id}/ai/coverage")
def get_ai_detection_coverage(camera_id: str, _: CurrentUser) -> dict[str, object]:
    _ensure_ai_settings(camera_id)
    with connect() as db:
        rows = db.execute("SELECT state,starts_at,ends_at,detail FROM ai_coverage WHERE camera_id=? ORDER BY starts_at DESC LIMIT 500", (camera_id,)).fetchall()
        setting = db.execute("SELECT status,last_attempt_at,last_success_at,last_error FROM ai_camera_settings WHERE camera_id=?", (camera_id,)).fetchone()
    return {"status": setting["status"], "last_attempt_at": setting["last_attempt_at"], "last_success_at": setting["last_success_at"], "last_error": setting["last_error"], "intervals": [dict(row) for row in rows], "coverage_note": "A failed or delayed interval is unknown coverage; detections only describe sampled frames."}


@app.delete("/api/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_camera(
    camera_id: str,
    request: Request,
    user: Administrator,
) -> Response:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        camera = db.execute("SELECT archived_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if camera is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if camera["archived_at"]:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        archived_at = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE cameras SET archived_at=?,enabled=0,recording_enabled=0,recording_status='archived',last_error=NULL,updated_at=? WHERE id=?",
            (archived_at, archived_at, camera_id),
        )
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "archive", "camera", camera_id, "{}"))
    try:
        # Archive state prevents new ingest after restart and the existing task is now cancelled.
        await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    except Exception:
        # The durable enabled=0 flag keeps the recorder supervisor from restarting ingest.
        raise
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/cameras/{camera_id}/restore", status_code=status.HTTP_200_OK)
def restore_camera(
    camera_id: str,
    user: Administrator,
) -> dict[str, object]:
    with connect() as db:
        row = db.execute("SELECT archived_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if row["archived_at"] is None:
            raise HTTPException(status_code=409, detail="Camera is not archived")
        db.execute("UPDATE cameras SET archived_at=NULL,enabled=1,recording_status='stopped',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (camera_id,))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "restore", "camera", camera_id, "{}"))
        updated = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    return _camera_view(updated)


@app.delete("/api/cameras/{camera_id}/footage", status_code=status.HTTP_204_NO_CONTENT)
def delete_archived_camera_footage(
    camera_id: str,
    payload: CameraFootageDeleteRequest,
    user: Administrator,
) -> Response:
    from .retention import unlink_many_if_unleased
    from .storage import recording_file_path

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        camera = db.execute("SELECT name,archived_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if camera is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if camera["archived_at"] is None:
            raise HTTPException(status_code=409, detail="Archive the camera before deleting its footage")
        if payload.confirm_camera_name != camera["name"]:
            raise HTTPException(status_code=422, detail="Camera name confirmation does not match")
        active_hold = db.execute(
            "SELECT 1 FROM recording_holds WHERE camera_id=? AND released_at IS NULL AND (expires_at IS NULL OR expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) LIMIT 1",
            (camera_id,),
        ).fetchone()
        if active_hold:
            raise HTTPException(status_code=409, detail="Archived camera has preserved footage; release its holds before deleting footage")
        active_export = db.execute(
            "SELECT 1 FROM export_sources s JOIN recording_segments r ON r.id=s.segment_id "
            "JOIN exports e ON e.id=s.export_id WHERE r.camera_id=? AND e.status IN ('queued','running') LIMIT 1",
            (camera_id,),
        ).fetchone()
        if active_export:
            raise HTTPException(status_code=409, detail="Archived camera footage is in an active export")
        segments = db.execute("SELECT id,relative_path FROM recording_segments WHERE camera_id=?", (camera_id,)).fetchall()
        try:
            paths = [recording_file_path(row["relative_path"]) for row in segments]
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Assigned recording storage is unavailable") from exc
        if not unlink_many_if_unleased(paths):
            raise HTTPException(status_code=409, detail="Archived camera footage is currently being played")
        db.execute("DELETE FROM recording_segments WHERE camera_id=?", (camera_id,))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "delete_camera_footage", "camera", camera_id,
                    json.dumps({"segments_deleted": len(segments)}, separators=(",", ":"))))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/cameras/{camera_id}/segments")
def list_camera_segments(
    camera_id: str,
    _: CurrentUser,
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
) -> dict[str, object]:
    with connect() as db:
        camera = db.execute("SELECT id, recording_status FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if not camera:
            raise HTTPException(status_code=404, detail="Camera not found")
        if (start is None) != (end is None):
            raise HTTPException(status_code=422, detail="Provide both from and to, or neither")
        if start is None or end is None:
            rows = db.execute(
                "SELECT s.id,s.starts_at,s.ends_at,s.size_bytes,(SELECT h.id FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) ORDER BY h.created_at DESC LIMIT 1) AS hold_id,(SELECT h.expires_at FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) ORDER BY h.created_at DESC LIMIT 1) AS hold_expires_at FROM recording_segments s WHERE camera_id=? ORDER BY starts_at DESC LIMIT 500",
                (camera_id,),
            ).fetchall()
        else:
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                raise HTTPException(status_code=422, detail="from and to must be timezone-aware and form a positive interval")
            rows = db.execute(
                "SELECT s.id,s.starts_at,s.ends_at,s.size_bytes,(SELECT h.id FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) ORDER BY h.created_at DESC LIMIT 1) AS hold_id,(SELECT h.expires_at FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) ORDER BY h.created_at DESC LIMIT 1) AS hold_expires_at FROM recording_segments s "
                "WHERE camera_id=? AND starts_at<? AND ends_at>? ORDER BY starts_at",
                (camera_id, end.astimezone(timezone.utc).isoformat(), start.astimezone(timezone.utc).isoformat()),
            ).fetchall()
    segments = [
        {"id": row["id"], "starts_at": row["starts_at"], "ends_at": row["ends_at"], "size_bytes": row["size_bytes"], "preserved":row["hold_id"] is not None,"hold_expires_at":row["hold_expires_at"],
         "media_url": f"/api/cameras/{camera_id}/segments/{row['id']}/media"}
        for row in rows
    ]
    return {
        "camera_id": camera_id,
        "recording_status": camera["recording_status"],
        "availability": "available" if segments else "none",
        "reason": None if segments else "No indexed footage covers this interval",
        "segments": segments,
    }


@app.get("/api/cameras/{camera_id}/coverage")
def list_camera_coverage(
    camera_id: str,
    _: CurrentUser,
) -> dict[str, object]:
    with connect() as db:
        camera = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if not camera:
            raise HTTPException(status_code=404, detail="Camera not found")
        policy = effective_policy(db, camera_id, datetime.now(timezone.utc))
        health = recording_health(
            enabled=bool(camera["enabled"]), recording_enabled=bool(camera["recording_enabled"]),
            mode=camera["recording_mode"], effective_mode=str(policy["mode"]),
            recording_status=camera["recording_status"], motion_source_status=camera["motion_source_status"],
            last_video_at=camera["last_video_at"], last_footage_write_at=camera["last_footage_write_at"],
        )
        rows = db.execute(
            "SELECT coverage_kind,reason,starts_at,ends_at FROM coverage_intervals WHERE camera_id=? ORDER BY starts_at DESC LIMIT 200",
            (camera_id,),
        ).fetchall()
    return {"camera_id": camera_id, "current": health, "intervals": [dict(row) for row in reversed(rows)]}


@app.get("/api/incidents")
def get_incidents(
    _: CurrentUser,
    state: str | None = Query(default=None, pattern="^(active|acknowledged|recovered)$"),
) -> dict[str, object]:
    with connect() as db:
        return {"incidents": list_incidents(db, state=state)}


@app.post("/api/incidents/{incident_id}/acknowledge")
def post_incident_acknowledgement(
    incident_id: str,
    user: CurrentUser,
) -> dict[str, object]:
    with connect() as db:
        incident = acknowledge_incident(db, incident_id, int(user["id"]))
        if incident is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        if incident.pop("acknowledgement_conflict", False):
            raise HTTPException(status_code=409, detail="Incident has already recovered")
        row = db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        return incident_view(db, row)


@app.get("/api/webhooks/event-types")
def get_webhook_event_types(_: Administrator) -> dict[str, object]:
    return {"event_types": sorted(WEBHOOK_EVENT_TYPES)}


@app.get("/api/heartbeat")
def get_heartbeat_status(_: Administrator) -> dict[str, object]:
    return heartbeat_status()


@app.put("/api/heartbeat")
def put_heartbeat_config(payload: HeartbeatConfigRequest, user: Administrator) -> dict[str, object]:
    try:
        result, secret = configure_heartbeat(payload.target_url, payload.interval_seconds, payload.allow_private_receiver, int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "heartbeat_configured", "heartbeat", "1", json.dumps({"interval_seconds": payload.interval_seconds, "allow_private_receiver": payload.allow_private_receiver})))
    return {"heartbeat": result, "signing_secret": secret, "secret_notice": "A new signing secret is shown once. Store it at the external receiver." if secret else None}


@app.post("/api/heartbeat/test", status_code=status.HTTP_202_ACCEPTED)
def post_heartbeat_test(user: Administrator) -> dict[str, str]:
    if not queue_heartbeat_test():
        raise HTTPException(status_code=409, detail="Configure and enable an external heartbeat receiver first")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "heartbeat_test_requested", "heartbeat", "1", "{}"))
    return {"status": "queued"}


@app.post("/api/heartbeat/rotate-secret")
def post_heartbeat_secret_rotation(user: Administrator) -> dict[str, str]:
    secret = rotate_heartbeat_secret()
    if secret is None:
        raise HTTPException(status_code=409, detail="Configure an enabled heartbeat receiver first")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "heartbeat_secret_rotated", "heartbeat", "1", "{}"))
    return {"signing_secret": secret, "secret_notice": "This secret is shown once. Update the receiver before the next heartbeat."}


@app.delete("/api/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
def delete_heartbeat_config(user: Administrator) -> Response:
    disable_heartbeat()
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "heartbeat_disabled", "heartbeat", "1", "{}"))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/webhooks")
def get_webhooks(_: Administrator) -> dict[str, object]:
    return {"webhooks": list_webhook_endpoints()}


@app.post("/api/webhooks", status_code=status.HTTP_201_CREATED)
def post_webhook(payload: WebhookCreateRequest, user: Administrator) -> dict[str, object]:
    try:
        endpoint, secret = create_webhook_endpoint(payload.name, payload.url, payload.events, payload.allow_private_receiver, int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_created", "webhook", endpoint["id"], json.dumps({"name": endpoint["name"], "events": endpoint["events"], "allow_private_receiver": endpoint["allow_private_receiver"]})))
    return {"webhook": endpoint, "signing_secret": secret, "secret_notice": "This secret is shown once. Store it at the receiver to verify signed deliveries."}


@app.patch("/api/webhooks/{webhook_id}")
def patch_webhook(webhook_id: str, payload: WebhookUpdateRequest, user: Administrator) -> dict[str, object]:
    fields = payload.model_dump(exclude_unset=True)
    try:
        endpoint = update_webhook_endpoint(webhook_id, fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_updated", "webhook", webhook_id, json.dumps({"fields": sorted(fields), "events": endpoint["events"], "enabled": endpoint["enabled"]})))
    return {"webhook": endpoint}


@app.post("/api/webhooks/{webhook_id}/rotate-secret")
def post_webhook_secret_rotation(webhook_id: str, user: Administrator) -> dict[str, str]:
    secret = rotate_webhook_secret(webhook_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_secret_rotated", "webhook", webhook_id, "{}"))
    return {"signing_secret": secret, "secret_notice": "This secret is shown once. Update the receiver before the next delivery."}


@app.delete("/api/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_webhook(webhook_id: str, user: Administrator) -> Response:
    if not delete_webhook_endpoint(webhook_id):
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_deleted", "webhook", webhook_id, "{}"))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/webhooks/{webhook_id}/test", status_code=status.HTTP_202_ACCEPTED)
def post_webhook_test(webhook_id: str, user: Administrator) -> dict[str, object]:
    event_id = enqueue_webhook_test(webhook_id, int(user["id"]))
    if event_id is None:
        raise HTTPException(status_code=404, detail="Enabled webhook endpoint not found")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_test_requested", "webhook", webhook_id, json.dumps({"event_id": event_id})))
    return {"event_id": event_id, "status": "queued"}


@app.get("/api/webhooks/deliveries")
def get_webhook_deliveries(
    _: Administrator,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    return {"deliveries": list_webhook_deliveries(limit)}


@app.post("/api/playback/timeline")
def synchronized_playback_timeline(
    payload: SynchronizedPlaybackRequest,
    _: CurrentUser,
) -> dict[str, object]:
    start = payload.start.astimezone(timezone.utc)
    end = payload.end.astimezone(timezone.utc)
    start_text, end_text = start.isoformat(), end.isoformat()
    cameras: list[dict[str, object]] = []
    with connect() as db:
        for camera_id in payload.camera_ids:
            camera = db.execute("SELECT id,name,recording_status,audio_supported,audio_enabled FROM cameras WHERE id=?", (camera_id,)).fetchone()
            if camera is None:
                raise HTTPException(status_code=422, detail="Playback request contains an unknown camera")
            segment_rows = db.execute(
                "SELECT id,starts_at,ends_at,size_bytes FROM recording_segments "
                "WHERE camera_id=? AND starts_at<? AND ends_at>? ORDER BY starts_at LIMIT 500",
                (camera_id, end_text, start_text),
            ).fetchall()
            segments = [{
                "id": row["id"], "starts_at": row["starts_at"], "ends_at": row["ends_at"],
                "size_bytes": row["size_bytes"],
                "media_url": f"/api/cameras/{camera_id}/segments/{row['id']}/media",
            } for row in segment_rows]
            coverage_rows = db.execute(
                "SELECT coverage_kind,reason,starts_at,ends_at FROM coverage_intervals "
                "WHERE camera_id=? AND starts_at<? AND (ends_at IS NULL OR ends_at>?) ORDER BY starts_at LIMIT 500",
                (camera_id, end_text, start_text),
            ).fetchall()
            coverage = [dict(row) for row in coverage_rows]
            event_rows = db.execute(
                "SELECT id,source_event_at,requested_start,requested_end,actual_start,actual_end,"
                "pre_roll_complete,status,failure_reason,segment_id FROM motion_events "
                "WHERE camera_id=? AND source_event_at>=? AND source_event_at<? ORDER BY source_event_at LIMIT 1000",
                (camera_id, start_text, end_text),
            ).fetchall()
            events = [{
                **dict(row), "pre_roll_complete": bool(row["pre_roll_complete"]),
                "media_url": f"/api/cameras/{camera_id}/segments/{row['segment_id']}/media" if row["segment_id"] else None,
            } for row in event_rows]
            spans = sorted((max(start, datetime.fromisoformat(row["starts_at"].replace("Z", "+00:00")).astimezone(timezone.utc)),
                            min(end, datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00")).astimezone(timezone.utc)))
                           for row in segment_rows)
            gaps: list[dict[str, object]] = []
            def append_gap(gap_start: datetime, gap_end: datetime) -> None:
                if gap_end <= gap_start:
                    return
                boundaries = {gap_start, gap_end}
                normalized = []
                for item in coverage:
                    item_start = datetime.fromisoformat(item["starts_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
                    item_end = datetime.fromisoformat(item["ends_at"].replace("Z", "+00:00")).astimezone(timezone.utc) if item["ends_at"] else end
                    item_start, item_end = max(gap_start, item_start), min(gap_end, item_end)
                    if item_end > item_start:
                        boundaries.update((item_start, item_end))
                        normalized.append((item_start, item_end, item))
                ordered = sorted(boundaries)
                for piece_start, piece_end in zip(ordered, ordered[1:]):
                    midpoint = piece_start + (piece_end - piece_start) / 2
                    active = [item for item_start, item_end, item in normalized if item_start <= midpoint < item_end]
                    kinds = {item["coverage_kind"] for item in active}
                    kind = "failure" if "failure" in kinds else "uncertain" if "uncertain" in kinds or "covered" in kinds else "expected_idle" if "expected_idle" in kinds else "missing"
                    reason = next((item["reason"] for item in active if item["coverage_kind"] == kind),
                                  "Coverage is uncertain where recording is expected" if kind == "uncertain" else "No indexed footage covers this interval")
                    gaps.append({"starts_at": piece_start.isoformat(), "ends_at": piece_end.isoformat(), "state": kind, "reason": reason})
            cursor = start
            for span_start, span_end in spans:
                if span_start > cursor:
                    append_gap(cursor, span_start)
                cursor = max(cursor, span_end)
            if cursor < end:
                append_gap(cursor, end)
            cameras.append({"camera_id": camera_id, "name": camera["name"], "recording_status": camera["recording_status"],
                            "audio_supported": None if camera["audio_supported"] is None else bool(camera["audio_supported"]), "audio_enabled": bool(camera["audio_enabled"]),
                            "segments": segments, "coverage": coverage, "motion_events": events, "gaps": gaps})
    return {"start": start_text, "end": end_text, "cameras": cameras}


@app.get("/api/cameras/{camera_id}/segments/{segment_id}/hold")
def segment_hold_status(camera_id: str, segment_id: int, _: CurrentUser) -> dict[str, object]:
    try:
        return get_segment_hold(camera_id,segment_id)
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc


@app.post("/api/cameras/{camera_id}/segments/{segment_id}/hold",status_code=status.HTTP_201_CREATED)
def preserve_camera_segment(camera_id: str, segment_id: int, payload: HoldRequest, user: CurrentUser) -> dict[str, object]:
    if payload.expires_in_days is not None and user["role"]!="administrator":
        raise HTTPException(status_code=403,detail="Only administrators can set a hold expiry")
    try:
        return preserve_segment(camera_id,segment_id,int(user["id"]),payload.expires_in_days)
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=410,detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503,detail="Assigned storage volume is unavailable") from exc


@app.delete("/api/cameras/{camera_id}/segments/{segment_id}/hold",status_code=status.HTTP_204_NO_CONTENT)
def release_camera_segment(camera_id: str, segment_id: int, user: Administrator) -> Response:
    try:
        release_segment_hold(camera_id,segment_id,int(user["id"]))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    return Response(status_code=204)


@app.delete("/api/cameras/{camera_id}/segments/{segment_id}",status_code=status.HTTP_204_NO_CONTENT)
def delete_camera_segment(camera_id: str, segment_id: int, user: Administrator) -> Response:
    try:
        delete_preserved_segment(camera_id,segment_id,int(user["id"]))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    except BlockingIOError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503,detail="Assigned storage volume is unavailable") from exc
    return Response(status_code=204)


@app.get("/api/cameras/{camera_id}/segments/{segment_id}/media")
def play_segment(
    camera_id: str,
    segment_id: int,
    _: CurrentUser,
) -> FileResponse:
    with connect() as db:
        row = db.execute(
            "SELECT relative_path FROM recording_segments WHERE id=? AND camera_id=?",
            (segment_id, camera_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Recorded interval is unavailable")
    from .storage import recording_file_path
    try:
        path = recording_file_path(row["relative_path"])
    except (OSError, ValueError):
        raise HTTPException(status_code=410, detail="Recorded interval is unavailable")
    acquire_playback(path)
    if not path.is_file():
        release_playback(path)
        raise HTTPException(status_code=410, detail="Recorded interval is unavailable")
    return FileResponse(path, media_type="video/mp4", filename=path.name, content_disposition_type="inline", background=BackgroundTask(release_playback,path))


@app.get("/api/cameras/{camera_id}/segments/{segment_id}/snapshot")
async def snapshot_segment(
    camera_id: str,
    segment_id: int,
    at: datetime,
    user: CurrentUser,
) -> Response:
    if at.tzinfo is None:
        raise HTTPException(status_code=422, detail="Snapshot time must include a timezone")
    with connect() as db:
        row = db.execute("SELECT relative_path,starts_at,ends_at FROM recording_segments WHERE id=? AND camera_id=?", (segment_id,camera_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Recorded interval is unavailable")
    start = datetime.fromisoformat(row["starts_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    end = datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    at = at.astimezone(timezone.utc)
    if at < start or at >= end:
        raise HTTPException(status_code=422, detail="Snapshot time is outside this recorded interval")
    if not shutil.which("ffmpeg"):
        raise HTTPException(status_code=503, detail="Snapshot extraction is unavailable")
    from .storage import recording_file_path
    try:
        path = recording_file_path(row["relative_path"])
    except (OSError, ValueError):
        raise HTTPException(status_code=410, detail="Recorded interval is unavailable")
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Recorded interval is unavailable")
    acquire_playback(path)
    offset = max(0.0, (at - start).total_seconds())
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{offset:.3f}",
            "-i", str(path), "-frames:v", "1", "-vf", "scale=1280:-2:force_original_aspect_ratio=decrease",
            "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        image, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        if process.returncode != 0 or not image.startswith(b"\xff\xd8"):
            raise HTTPException(status_code=422, detail="A snapshot could not be extracted from this interval")
        source_time = at.isoformat()
        with connect() as db:
            cursor = db.execute(
                "INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                (int(user["id"]), "snapshot_downloaded", "recording_segment", str(segment_id),
                 json.dumps({"camera_id": camera_id, "source_time": source_time})),
            )
            audit_reference = f"audit-{cursor.lastrowid}"
        return Response(image, media_type="image/jpeg", headers={
            "Cache-Control": "no-store", "X-Source-Time": source_time, "X-Audit-Reference": audit_reference,
        })
    except asyncio.TimeoutError as exc:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        raise HTTPException(status_code=503, detail="Snapshot extraction timed out") from exc
    finally:
        release_playback(path)


@app.get("/api/cameras/{camera_id}/segments/{segment_id}/frame-neighbor")
def playback_frame_neighbor(
    camera_id: str,
    segment_id: int,
    at: datetime,
    direction: Literal["previous", "next"],
    _: CurrentUser,
) -> dict[str, str]:
    """Return an adjacent decoded video frame timestamp from a protected recording."""
    if at.tzinfo is None:
        raise HTTPException(status_code=422, detail="Frame time must include a timezone")
    with connect() as db:
        row = db.execute("SELECT relative_path,starts_at,ends_at FROM recording_segments WHERE id=? AND camera_id=?", (segment_id, camera_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Recorded interval is unavailable")
    start = datetime.fromisoformat(row["starts_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    end = datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    at = at.astimezone(timezone.utc)
    if at < start or at >= end:
        raise HTTPException(status_code=422, detail="Frame time is outside this recorded interval")
    if not shutil.which("ffprobe"):
        raise HTTPException(status_code=503, detail="Frame stepping is unavailable")
    from .storage import recording_file_path
    try:
        path = recording_file_path(row["relative_path"])
    except (OSError, ValueError):
        raise HTTPException(status_code=410, detail="Recorded interval is unavailable")
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Recorded interval is unavailable")
    acquire_playback(path)
    try:
        offset = max(0.0, (at - start).total_seconds())
        window_start = max(0.0, offset - 2.0) if direction == "previous" else offset
        read_duration = offset - window_start + 2.0 if direction == "previous" else 2.0
        try:
            result = subprocess.run(
                [shutil.which("ffprobe"), "-v", "error", "-read_intervals", f"{window_start:.3f}%+{read_duration:.3f}",
                 "-select_streams", "v:0", "-show_frames", "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path)],
                capture_output=True, text=True, timeout=12, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=503, detail="Frame timestamp extraction timed out") from exc
        if result.returncode:
            raise HTTPException(status_code=422, detail="Frame timestamps could not be read")
        try:
            frames = json.loads(result.stdout).get("frames", [])
            frame_times = sorted({start.timestamp() + float(frame["best_effort_timestamp_time"]) for frame in frames
                                  if frame.get("best_effort_timestamp_time") is not None})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="Frame timestamps could not be read") from exc
        frame_times = [value for value in frame_times if start.timestamp() <= value < end.timestamp()]
        target = at.timestamp()
        candidates = [value for value in frame_times if value < target] if direction == "previous" else [value for value in frame_times if value > target]
        if not candidates:
            raise HTTPException(status_code=404, detail="No adjacent frame exists in this interval")
        selected = max(candidates) if direction == "previous" else min(candidates)
        return {"at": datetime.fromtimestamp(selected, timezone.utc).isoformat()}
    finally:
        release_playback(path)


@app.get("/api/exports")
def get_exports(user: CurrentUser) -> dict[str, object]:
    return {"exports": list_exports(int(user["id"]), user["role"] == "administrator")}


@app.post("/api/exports", status_code=status.HTTP_202_ACCEPTED)
async def create_export(
    payload: ExportRequest,
    request: Request,
    user: CurrentUser,
) -> dict[str, object]:
    start, end = payload.start.astimezone(timezone.utc), payload.end.astimezone(timezone.utc)
    try:
        job_id = create_export_record(int(user["id"]), payload.camera_ids, start, end, payload.output_modes, payload.tile_camera_ids, payload.grid_resolution, payload.include_audio, payload.audio_camera_id)
    except LookupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        request.app.state.exports.enqueue(job_id)
    except (AttributeError, RuntimeError) as exc:
        with connect() as db:
            db.execute("UPDATE exports SET status='failed',error=?,completed_at=?,updated_at=? WHERE id=?", ("Export worker is unavailable", datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), job_id))
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)", (int(user["id"]), "export_failed", "export", job_id, '{"reason":"worker_unavailable"}'))
        raise HTTPException(status_code=503, detail="Export worker is unavailable") from exc
    row = get_export(job_id, int(user["id"]))
    return _job_view(row)


@app.get("/api/exports/{job_id}")
def get_export_status(job_id: str, user: CurrentUser) -> dict[str, object]:
    row = get_export(job_id, int(user["id"]), user["role"] == "administrator")
    if row is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    return _job_view(row)


@app.post("/api/exports/{job_id}/cancel")
async def cancel_export(job_id: str, request: Request, user: CurrentUser) -> dict[str, object]:
    try:
        state = request_cancel(job_id, int(user["id"]), user["role"] == "administrator")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if state == "cancel_requested":
        await request.app.state.exports.cancel(job_id)
    row = get_export(job_id, int(user["id"]), user["role"] == "administrator")
    return _job_view(row)


@app.get("/api/exports/{job_id}/download")
def download_export(job_id: str, user: CurrentUser) -> FileResponse:
    row = get_export(job_id, int(user["id"]), user["role"] == "administrator")
    if row is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    if row["status"] == "completed" and row["expires_at"] and datetime.now(timezone.utc) >= datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")):
        from .exports import expire_exports
        expire_exports()
        raise HTTPException(status_code=410, detail="Evidence package expired after seven days")
    try:
        path = export_output_path(row)
    except FileNotFoundError as exc:
        status_code = 410 if row["status"] == "completed" else 409
        raise HTTPException(status_code=status_code, detail="Evidence package is unavailable" if status_code == 410 else str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=410, detail="Export file is unavailable") from exc
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Export file is unavailable")
    acquire_playback(path)
    return FileResponse(path, media_type="application/zip", filename=f"nvr-export-{job_id}.zip", background=BackgroundTask(release_playback, path))


@app.post("/api/exports/{job_id}/verify")
async def verify_export_evidence(job_id: str, request: Request, user: CurrentUser) -> dict[str, object]:
    """Check manifest checksums against uploaded package bytes; media probing is separate."""
    row = get_export(job_id, int(user["id"]), user["role"] == "administrator")
    if row is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    if row["status"] != "completed":
        raise HTTPException(status_code=409, detail="Evidence package is not ready")
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_VERIFY_PACKAGE_BYTES:
        raise HTTPException(status_code=413, detail="Evidence package exceeds the verification size limit")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_VERIFY_PACKAGE_BYTES:
            raise HTTPException(status_code=413, detail="Evidence package exceeds the verification size limit")
    result = verify_evidence_bytes(bytes(body), job_id)
    action = "evidence_verified" if result["checksums_valid"] else "evidence_verification_failed"
    with connect() as db:
        db.execute(
            "INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
            (int(user["id"]), action, "export", job_id, json.dumps({"outcome": "valid" if result["checksums_valid"] else "invalid", "file_count": len(result["files"])})),
        )
    return {**result, "media_validation": "not_performed", "authenticity": "Checksums are unsigned. A matching manifest detects accidental or subsequent byte changes but does not prove package origin, creation time, or camera timestamp accuracy."}
