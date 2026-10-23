"""Camera inventory, stream sources, credentials, audio probing and motion sources."""
from __future__ import annotations

import sqlite3
import uuid
import json
import time
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator

from app.principal import Administrator, CurrentUser
from app.store import connect
from app.credentials import encrypt, decrypt
from app.recording import masked_stream_url, probe_stream, safe_stream_url
from app.health import recording_health
from app.onvif_stream import list_camera_stream_profiles, resolve_camera_stream
from app import diagnostics
from app.retention import coverage_summary, effective_retention
from app.api.policies import effective_schedule_view

router = APIRouter()


CameraBrand = Literal["auto", "acti", "axis", "bosch", "dahua", "hanwha", "hikvision", "reolink", "samsung", "uniview", "other"]


class CameraSourceRequest(BaseModel):
    stream_url: str | None = Field(default=None, min_length=8, max_length=2048)
    camera_ip: str | None = Field(default=None, min_length=7, max_length=45)
    camera_username: str | None = Field(default=None, max_length=256)
    camera_password: str | None = Field(default=None, max_length=1024)
    camera_brand: CameraBrand = "auto"
    onvif_profile_token: str | None = Field(default=None, max_length=256)
    live_substream_profile_token: str | None = Field(default=None, max_length=256)
    device_service_url: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_source(self) -> "CameraSourceRequest":
        if bool(self.stream_url) == bool(self.camera_ip):
            raise ValueError("Provide either a camera IP address or an RTSP stream URL")
        if self.live_substream_profile_token and (not self.camera_ip or not self.onvif_profile_token):
            raise ValueError("A live substream requires an ONVIF camera address and a selected main profile")
        if self.live_substream_profile_token and self.live_substream_profile_token == self.onvif_profile_token:
            raise ValueError("Choose a different profile for the optional live substream")
        return self

    @field_validator("device_service_url")
    @classmethod
    def validate_device_service_url(cls, value: str | None) -> str | None:
        return _validate_onvif_service_url(value)


class CameraProbeRequest(CameraSourceRequest):
    @model_validator(mode="after")
    def validate_credentials(self) -> "CameraProbeRequest":
        if bool(self.camera_username) != bool(self.camera_password):
            raise ValueError("Provide both camera username and password, or leave both blank")
        return self


class CameraSourceUpdateRequest(CameraSourceRequest):
    clear_credentials: bool = False

    @model_validator(mode="after")
    def validate_credential_choice(self) -> "CameraSourceUpdateRequest":
        if self.clear_credentials and (self.camera_username or self.camera_password):
            raise ValueError("Clear saved credentials or provide new credentials, not both")
        if self.clear_credentials and self.stream_url:
            parsed = urlsplit(self.stream_url)
            if parsed.username or parsed.password:
                raise ValueError("Remove credentials from the direct stream URL before clearing saved credentials")
        return self


class CameraProfileRequest(BaseModel):
    camera_ip: str = Field(min_length=7, max_length=45)
    camera_username: str | None = Field(default=None, max_length=256)
    camera_password: str | None = Field(default=None, max_length=1024)
    camera_brand: CameraBrand = "auto"
    device_service_url: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_credentials(self) -> "CameraProfileRequest":
        if bool(self.camera_username) != bool(self.camera_password):
            raise ValueError("Provide both camera username and password, or leave both blank")
        return self


def _validate_onvif_service_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("ONVIF motion service must be an HTTP(S) device-service URL without credentials, query, or fragment")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid ONVIF service port")
    return value.strip()


class MotionZoneRequest(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def fit_frame(self) -> "MotionZoneRequest":
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("Motion zone must fit inside the normalized frame")
        return self


class CameraCreateRequest(CameraProbeRequest):
    name: str = Field(min_length=1, max_length=120)
    recording_mode: Literal["motion", "continuous", "disabled"] = "motion"
    recording_enabled: bool = False
    motion_service_url: str | None = Field(default=None, max_length=2048)
    motion_username: str | None = Field(default=None, max_length=256)
    motion_password: str | None = Field(default=None, max_length=1024)
    motion_pre_roll_seconds: int = Field(default=10, ge=0, le=120)
    motion_post_roll_seconds: int = Field(default=30, ge=1, le=600)
    motion_fallback_enabled: bool = False
    motion_zones: list[MotionZoneRequest] = Field(default_factory=list, max_length=16)
    motion_sensitivity: int = Field(default=50, ge=1, le=100)
    onvif_uuid: str | None = Field(default=None, min_length=36, max_length=36)

    @field_validator("onvif_uuid")
    @classmethod
    def validate_onvif_uuid(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ValueError("ONVIF identity must be a UUID") from exc

    @field_validator("motion_service_url")
    @classmethod
    def validate_motion_service_url(cls, value: str | None) -> str | None:
        return _validate_onvif_service_url(value)

    @field_validator("motion_username", "motion_password")
    @classmethod
    def trim_motion_credentials(cls, value: str | None) -> str | None:
        return value if value else None


class CameraCredentialRotateRequest(CameraProbeRequest):
    pass


class MotionSourceUpdateRequest(BaseModel):
    service_url: str | None = Field(default=None, max_length=2048)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=1024)
    pre_roll_seconds: int | None = Field(default=None, ge=0, le=120)
    post_roll_seconds: int | None = Field(default=None, ge=1, le=600)
    fallback_enabled: bool | None = None
    zones: list[MotionZoneRequest] | None = Field(default=None, max_length=16)
    sensitivity: int | None = Field(default=None, ge=1, le=100)

    @field_validator("service_url")
    @classmethod
    def validate_service_url(cls, value: str | None) -> str | None:
        validated = _validate_onvif_service_url(value)
        return validated


class CameraUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    recording_mode: Literal["motion", "continuous", "disabled"] | None = None
    recording_enabled: bool | None = None
    motion_pre_roll_seconds: int | None = Field(default=None, ge=0, le=120)
    motion_post_roll_seconds: int | None = Field(default=None, ge=1, le=600)
    audio_enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str | None) -> str | None:
        value = value.strip() if value is not None else None
        if value == "":
            raise ValueError("Camera name is required")
        return value

    @field_validator("recording_mode")
    @classmethod
    def require_update_fields(cls, value: str | None) -> str | None:
        return value


@router.post("/api/cameras/onvif-profiles")
async def camera_onvif_profiles(
    payload: CameraProfileRequest,
    _: Administrator,
) -> dict[str, object]:
    try:
        service_url, profiles = await list_camera_stream_profiles(
            payload.camera_ip, payload.camera_username or "", payload.camera_password or "",
            payload.device_service_url, payload.camera_brand,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"device_service_url": service_url, "profiles": [profile.__dict__ for profile in profiles]}


def camera_view(row: sqlite3.Row) -> dict[str, object]:
    try:
        visible_url = masked_stream_url(decrypt(row["stream_url_ciphertext"]))
    except RuntimeError:
        visible_url = "rtsp://credentials-unavailable"
    return {
        "id": row["id"],
        "name": row["name"],
        "group_name": row["group_name"] if "group_name" in row.keys() else None,
        "storage_volume_id": row["storage_volume_id"] if "storage_volume_id" in row.keys() else None,
        "camera_ip": row["camera_ip"] if "camera_ip" in row.keys() else None,
        "camera_brand": row["camera_brand"] if "camera_brand" in row.keys() else "auto",
        "device_service_url": row["device_service_url"] if "device_service_url" in row.keys() else None,
        "onvif_profile_token": row["onvif_profile_token"] if "onvif_profile_token" in row.keys() else None,
        "live_substream_profile_token": row["live_substream_profile_token"] if "live_substream_profile_token" in row.keys() else None,
        "archived_at": row["archived_at"] if "archived_at" in row.keys() else None,
        "stream_url": visible_url,
        "enabled": bool(row["enabled"]),
        "recording_mode": row["recording_mode"],
        "recording_enabled": bool(row["recording_enabled"]),
        "recording_status": row["recording_status"],
        "last_video_at": row["last_video_at"],
        "last_footage_write_at": row["last_footage_write_at"],
        "recording_fault_since": row["recording_fault_since"],
        "recording_health": recording_health(
            enabled=bool(row["enabled"]), recording_enabled=bool(row["recording_enabled"]),
            mode=row["recording_mode"], effective_mode=row["recording_mode"],
            recording_status=row["recording_status"], motion_source_status=row["motion_source_status"],
            last_video_at=row["last_video_at"], last_footage_write_at=row["last_footage_write_at"],
        ),
        "motion_source_status": row["motion_source_status"],
        "motion_source_configured": bool(row["motion_service_url"]),
        "motion_pre_roll_seconds": row["motion_pre_roll_seconds"],
        "motion_post_roll_seconds": row["motion_post_roll_seconds"],
        "motion_fallback_enabled": bool(row["motion_fallback_enabled"]),
        "motion_zones": json.loads(row["motion_zones_json"]),
        "motion_sensitivity": row["motion_sensitivity"],
        "server_motion_status": row["server_motion_status"],
        "video": {"codec": row["video_codec"], "width": row["video_width"], "height": row["video_height"]},
        "live_substream_available": bool(row["live_substream_url_ciphertext"]),
        "audio_supported": None if row["audio_supported"] is None else bool(row["audio_supported"]),
        "audio_enabled": bool(row["audio_enabled"]),
        "audio_status": "enabled" if row["audio_enabled"] else ("unknown" if row["audio_supported"] is None else ("available_disabled" if bool(row["audio_supported"]) else "unsupported")),
        "last_probe_at": row["last_probe_at"],
        "last_error": row["last_error"],
    }


def insert_camera(
    db: sqlite3.Connection,
    *,
    name: str,
    stream_url: str,
    recording_mode: str,
    capability: dict[str, object],
    onvif_uuid: str | None = None,
    group_id: str | None = None,
    camera_ip: str | None = None,
    camera_brand: str = "auto",
    device_service_url: str | None = None,
    onvif_profile_token: str | None = None,
    live_substream_profile_token: str | None = None,
    recording_enabled: bool = False,
    motion_service_url: str | None = None,
    motion_username: str | None = None,
    motion_password: str | None = None,
    motion_pre_roll_seconds: int = 10,
    motion_post_roll_seconds: int = 30,
    motion_fallback_enabled: bool = False,
    motion_zones: list[MotionZoneRequest] | None = None,
    motion_sensitivity: int = 50,
    live_substream_url: str | None = None,
) -> str:
    """Persist a camera only after its stream has passed the shared probe."""
    video = capability["video"]
    if not isinstance(video, dict):
        raise ValueError("A successful stream probe must include video metadata")
    camera_id = str(uuid.uuid4())
    recording_enabled = bool(recording_enabled and recording_mode != "disabled")
    status = "stopped" if not recording_enabled else ("connecting" if recording_mode == "continuous" or motion_service_url or motion_fallback_enabled else "unsupported")
    error = "Configure an ONVIF event service or enable server motion fallback" if recording_enabled and recording_mode == "motion" and not motion_service_url and not motion_fallback_enabled else None
    credentials = None
    if motion_username or motion_password:
        if not motion_username or not motion_password:
            raise ValueError("Provide both ONVIF motion username and password")
        credentials = encrypt(json.dumps({"username": motion_username, "password": motion_password}, separators=(",", ":")))
    db.execute(
        "INSERT INTO cameras(id,name,onvif_uuid,group_id,camera_ip,camera_brand,device_service_url,onvif_profile_token,live_substream_profile_token,stream_url_ciphertext,live_substream_url_ciphertext,audio_supported,audio_enabled,recording_mode,recording_enabled,recording_status,"
        "motion_service_url,motion_credentials_ciphertext,motion_pre_roll_seconds,motion_post_roll_seconds,motion_source_status," 
        "motion_fallback_enabled,motion_zones_json,motion_sensitivity,server_motion_status,last_error," 
        "video_codec,video_width,video_height,last_probe_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (camera_id, name, onvif_uuid, group_id, camera_ip, camera_brand, device_service_url, onvif_profile_token, live_substream_profile_token,
         encrypt(stream_url.strip()), encrypt(live_substream_url.strip()) if live_substream_url else None, int(bool(capability.get("audio"))), 0, recording_mode, int(recording_enabled), status,
         motion_service_url, credentials, motion_pre_roll_seconds, motion_post_roll_seconds,
         "untested" if motion_service_url else "unsupported", int(motion_fallback_enabled),
         json.dumps([zone.model_dump() for zone in (motion_zones or [])], separators=(",", ":")), motion_sensitivity,
         "waiting" if motion_fallback_enabled else "disabled", error,
         video.get("codec"), video.get("width"), video.get("height")),
    )
    return camera_id


def record_discovery_probe(db: sqlite3.Connection, device_uuid: str | None, capability: dict[str, object]) -> None:
    if not device_uuid:
        return
    row = db.execute("SELECT id,capabilities FROM discovery_candidates WHERE device_uuid=?", (device_uuid,)).fetchone()
    if row is None:
        return
    states = json.loads(row["capabilities"])
    states["video_stream"] = "supported"
    if isinstance(capability.get("audio"), bool):
        states["audio"] = "supported" if capability["audio"] else "unsupported"
    db.execute("UPDATE discovery_candidates SET capabilities=? WHERE id=?", (json.dumps(states), row["id"]))


def record_discovery_failure(db: sqlite3.Connection, device_uuid: str) -> None:
    row = db.execute("SELECT id,capabilities FROM discovery_candidates WHERE device_uuid=?", (device_uuid,)).fetchone()
    if row is None:
        return
    states = json.loads(row["capabilities"])
    states["video_stream"] = "unavailable"
    db.execute("UPDATE discovery_candidates SET capabilities=? WHERE id=?", (json.dumps(states), row["id"]))


@router.post("/api/cameras/test")
async def test_camera_stream(
    payload: CameraProbeRequest,
    _: Administrator,
) -> dict[str, object]:
    try:
        stream_url, _ = await _resolve_camera_source(payload)
        safe_stream_url(stream_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    started = time.monotonic()
    result = await probe_stream(stream_url)
    diagnostics.record_event(
        "stream_probe", outcome="success" if result.get("reachable") else "unavailable",
        elapsed_ms=min(60000, int((time.monotonic() - started) * 1000)),
    )
    return result


async def _resolve_camera_source(payload: CameraProbeRequest, service_url: str | None = None) -> tuple[str, str | None]:
    """Return an RTSP URI and its ONVIF device-service URL, resolving IP inputs safely."""
    if payload.stream_url:
        stream_url = payload.stream_url.strip()
        if payload.camera_username and payload.camera_password:
            parsed = urlsplit(stream_url)
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            if parsed.port:
                host = f"{host}:{parsed.port}"
            stream_url = urlunsplit((parsed.scheme, f"{quote(payload.camera_username, safe='')}:{quote(payload.camera_password, safe='')}@{host}", parsed.path, parsed.query, ""))
        return stream_url, service_url
    try:
        source = await resolve_camera_stream(
            payload.camera_ip or "", payload.camera_username or "", payload.camera_password or "",
            service_url or payload.device_service_url, payload.onvif_profile_token, payload.camera_brand,
        )
        return source.stream_url, source.device_service_url
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/cameras")
def list_cameras(_: CurrentUser) -> dict[str, object]:
    with connect() as db:
        rows = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id ORDER BY c.name COLLATE NOCASE").fetchall()
        cameras = []
        for row in rows:
            camera = camera_view(row)
            schedule = effective_schedule_view(db, row["id"])
            camera.update(schedule)
            retention_days, retention_source = effective_retention(db, row["id"])
            camera["retention_days"] = retention_days
            camera["retention_policy_source"] = retention_source
            camera["retention_coverage"] = coverage_summary(db, row["id"])
            camera["recording_health"] = recording_health(
                enabled=bool(row["enabled"]), recording_enabled=bool(row["recording_enabled"]),
                mode=row["recording_mode"], effective_mode=str(schedule["effective_recording_mode"]),
                recording_status=row["recording_status"], motion_source_status=row["motion_source_status"],
                last_video_at=row["last_video_at"], last_footage_write_at=row["last_footage_write_at"],
            )
            cameras.append(camera)
    return {"cameras": cameras}


@router.post("/api/cameras", status_code=status.HTTP_201_CREATED)
async def create_camera(
    payload: CameraCreateRequest,
    request: Request,
    _: Administrator,
) -> dict[str, object]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Camera name is required")
    try:
        stream_url, resolved_service_url = await _resolve_camera_source(payload, payload.motion_service_url)
        safe_stream_url(stream_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    motion_username = payload.motion_username if payload.motion_username is not None else payload.camera_username
    motion_password = payload.motion_password if payload.motion_password is not None else payload.camera_password
    if bool(motion_username) != bool(motion_password):
        raise HTTPException(status_code=422, detail="Provide both ONVIF motion username and password")
    capability = await probe_stream(stream_url)
    if not capability.get("reachable"):
        raise HTTPException(status_code=422, detail=str(capability.get("reason", "Camera stream could not be opened")))
    live_substream_url = None
    if payload.live_substream_profile_token:
        try:
            substream = await resolve_camera_stream(
                payload.camera_ip or "", payload.camera_username or "", payload.camera_password or "",
                resolved_service_url or payload.device_service_url, payload.live_substream_profile_token, payload.camera_brand,
            )
            safe_stream_url(substream.stream_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="The selected live substream could not be resolved") from exc
        substream_capability = await probe_stream(substream.stream_url)
        if not substream_capability.get("reachable"):
            raise HTTPException(status_code=422, detail="The selected live substream failed its video test")
        main_video = capability.get("video") or {}
        sub_video = substream_capability.get("video") or {}
        main_pixels = int(main_video.get("width") or 0) * int(main_video.get("height") or 0)
        sub_pixels = int(sub_video.get("width") or 0) * int(sub_video.get("height") or 0)
        if not main_pixels or not sub_pixels or sub_pixels >= main_pixels:
            raise HTTPException(status_code=422, detail="Choose an ONVIF profile with a lower video resolution for grid viewing")
        live_substream_url = substream.stream_url
    with connect() as db:
        try:
            discovery_claim = getattr(request.state, "discovery_add_claim", None)
            if discovery_claim:
                db.execute("BEGIN IMMEDIATE")
            camera_id = insert_camera(
                db, name=name, stream_url=stream_url, recording_mode=payload.recording_mode,
                capability=capability, onvif_uuid=payload.onvif_uuid,
                camera_ip=payload.camera_ip, camera_brand=payload.camera_brand,
                device_service_url=resolved_service_url or payload.device_service_url,
                onvif_profile_token=payload.onvif_profile_token,
                live_substream_profile_token=payload.live_substream_profile_token,
                recording_enabled=payload.recording_enabled,
                motion_service_url=payload.motion_service_url or resolved_service_url,
                motion_username=motion_username,
                motion_password=motion_password,
                motion_pre_roll_seconds=payload.motion_pre_roll_seconds,
                motion_post_roll_seconds=payload.motion_post_roll_seconds,
                motion_fallback_enabled=payload.motion_fallback_enabled,
                motion_zones=payload.motion_zones,
                motion_sensitivity=payload.motion_sensitivity,
                live_substream_url=live_substream_url,
            )
            record_discovery_probe(
                db,
                discovery_claim["device_uuid"] if discovery_claim else payload.onvif_uuid,
                capability,
            )
            if discovery_claim:
                claimed = db.execute(
                    "UPDATE discovery_candidates SET linked_camera_id=?,reviewed_at=?,add_claim_token=NULL,add_claimed_until=NULL "
                    "WHERE id=? AND add_claim_token=? AND linked_camera_id IS NULL",
                    (
                        camera_id,
                        datetime.now(timezone.utc).isoformat(),
                        discovery_claim["candidate_id"],
                        discovery_claim["token"],
                    ),
                )
                if claimed.rowcount != 1:
                    raise HTTPException(status_code=409, detail="This discovered device was linked or claimed by another request")
                db.commit()
        except sqlite3.IntegrityError as exc:
            if discovery_claim:
                db.rollback()
            raise HTTPException(status_code=409, detail="A camera with that name or ONVIF identity already exists") from exc
        except Exception:
            if discovery_claim:
                db.rollback()
            raise
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    with connect() as db:
        row = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    return camera_view(row)


def _reuse_source_credentials(current: sqlite3.Row, payload: CameraSourceUpdateRequest) -> CameraSourceUpdateRequest:
    if payload.camera_ip and payload.camera_ip == current["camera_ip"] and not payload.clear_credentials:
        try:
            existing = urlsplit(decrypt(current["stream_url_ciphertext"]))
            existing_user = unquote(existing.username or "")
            existing_password = unquote(existing.password or "")
        except (RuntimeError, ValueError):
            existing_user = existing_password = ""
        updates = {}
        if not payload.camera_username and existing_user:
            updates["camera_username"] = existing_user
        if not payload.camera_password and existing_password:
            updates["camera_password"] = existing_password
        if updates:
            payload = payload.model_copy(update=updates)
    if bool(payload.camera_username) != bool(payload.camera_password):
        raise HTTPException(status_code=422, detail="Provide both camera username and password, or leave both blank")
    return payload


@router.post("/api/cameras/{camera_id}/source/test")
async def test_camera_source_update(
    camera_id: str,
    payload: CameraSourceUpdateRequest,
    _: Administrator,
) -> dict[str, object]:
    with connect() as db:
        current = db.execute("SELECT camera_ip,stream_url_ciphertext FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if current is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    payload = _reuse_source_credentials(current, payload)
    started = time.monotonic()
    try:
        stream_url, _ = await _resolve_camera_source(payload)
        safe_stream_url(stream_url)
    except (ValueError, HTTPException) as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = await probe_stream(stream_url)
    diagnostics.record_event("stream_probe", outcome="success" if result.get("reachable") else "unavailable", elapsed_ms=min(60000, int((time.monotonic() - started) * 1000)))
    return result


@router.post("/api/cameras/{camera_id}/source/profiles")
async def list_camera_source_update_profiles(
    camera_id: str,
    payload: CameraSourceUpdateRequest,
    _: Administrator,
) -> dict[str, object]:
    if not payload.camera_ip:
        raise HTTPException(status_code=422, detail="Enter the camera IP address before loading ONVIF profiles")
    with connect() as db:
        current = db.execute("SELECT camera_ip,stream_url_ciphertext FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if current is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    payload = _reuse_source_credentials(current, payload)
    try:
        service_url, profiles = await list_camera_stream_profiles(
            payload.camera_ip, payload.camera_username or "", payload.camera_password or "",
            payload.device_service_url, payload.camera_brand,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"device_service_url": service_url, "profiles": [profile.__dict__ for profile in profiles]}


@router.put("/api/cameras/{camera_id}/source")
async def update_camera_source(
    camera_id: str,
    payload: CameraSourceUpdateRequest,
    request: Request,
    _: Administrator,
) -> dict[str, object]:
    with connect() as db:
        current = db.execute("SELECT camera_ip,stream_url_ciphertext FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if current is None:
        raise HTTPException(status_code=404, detail="Camera not found")

    payload = _reuse_source_credentials(current, payload)

    try:
        stream_url, resolved_service_url = await _resolve_camera_source(payload)
        safe_stream_url(stream_url)
    except (ValueError, HTTPException) as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    capability = await probe_stream(stream_url)
    if not capability.get("reachable"):
        raise HTTPException(status_code=422, detail=str(capability.get("reason", "Camera stream could not be opened")))

    live_substream_url = None
    if payload.live_substream_profile_token:
        try:
            substream = await resolve_camera_stream(
                payload.camera_ip or "", payload.camera_username or "", payload.camera_password or "",
                resolved_service_url or payload.device_service_url, payload.live_substream_profile_token, payload.camera_brand,
            )
            safe_stream_url(substream.stream_url)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="The selected live substream could not be resolved") from exc
        substream_capability = await probe_stream(substream.stream_url)
        if not substream_capability.get("reachable"):
            raise HTTPException(status_code=422, detail="The selected live substream failed its video test")
        main_video = capability.get("video") or {}
        sub_video = substream_capability.get("video") or {}
        main_pixels = int(main_video.get("width") or 0) * int(main_video.get("height") or 0)
        sub_pixels = int(sub_video.get("width") or 0) * int(sub_video.get("height") or 0)
        if not main_pixels or not sub_pixels or sub_pixels >= main_pixels:
            raise HTTPException(status_code=422, detail="Choose an ONVIF profile with a lower video resolution for grid viewing")
        live_substream_url = substream.stream_url

    selected_service_url = resolved_service_url or payload.device_service_url
    with connect() as db:
        db.execute(
            "UPDATE cameras SET camera_ip=?,camera_brand=?,device_service_url=?,onvif_profile_token=?,live_substream_profile_token=?,"
            "motion_service_url=COALESCE(?,motion_service_url),"
            "stream_url_ciphertext=?,live_substream_url_ciphertext=?,audio_supported=?,audio_enabled=CASE WHEN ?=0 THEN 0 ELSE audio_enabled END,"
            "video_codec=?,video_width=?,video_height=?,last_probe_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),last_error=NULL,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id=?",
            (payload.camera_ip, payload.camera_brand, selected_service_url, payload.onvif_profile_token,
             payload.live_substream_profile_token, selected_service_url, encrypt(stream_url.strip()),
             encrypt(live_substream_url.strip()) if live_substream_url else None,
             int(bool(capability.get("audio"))), int(bool(capability.get("audio"))),
             (capability.get("video") or {}).get("codec"), (capability.get("video") or {}).get("width"),
             (capability.get("video") or {}).get("height"), camera_id),
        )
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    with connect() as db:
        row = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    return camera_view(row)


@router.patch("/api/cameras/{camera_id}")
async def update_camera(
    camera_id: str,
    payload: CameraUpdateRequest,
    request: Request,
    user: Administrator,
) -> dict[str, object]:
    if "name" in payload.model_fields_set and payload.name is None:
        raise HTTPException(status_code=422, detail="Camera name is required")
    if not any(name in payload.model_fields_set for name in ("name", "recording_mode", "recording_enabled", "motion_pre_roll_seconds", "motion_post_roll_seconds", "audio_enabled")):
        raise HTTPException(status_code=422, detail="Specify a camera name, recording setting, buffer duration, or audio setting")
    with connect() as db:
        row = db.execute("SELECT recording_mode,recording_enabled,motion_service_url,motion_fallback_enabled,audio_supported,audio_enabled FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if payload.audio_enabled and row["audio_supported"] is not True and row["audio_supported"] != 1:
            message = "Probe the camera audio capability before enabling audio" if row["audio_supported"] is None else "This camera stream has no supported audio track"
            raise HTTPException(status_code=422, detail=message)
        name_only = payload.model_fields_set == {"name"}
        mode = payload.recording_mode or row["recording_mode"]
        enabled = payload.recording_enabled if payload.recording_enabled is not None else bool(row["recording_enabled"])
        if mode == "disabled":
            enabled = False
        if name_only:
            assignments = ["name=?", "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"]
            values: list[object] = [payload.name]
        else:
            assignments = ["recording_mode=?", "recording_enabled=?", "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"]
            values = [mode, int(enabled)]
            if payload.name is not None:
                assignments.append("name=?")
                values.append(payload.name)
        audio_enabled = payload.audio_enabled if payload.audio_enabled is not None else bool(row["audio_enabled"])
        if not name_only:
            if payload.audio_enabled is not None:
                assignments.append("audio_enabled=?"); values.append(int(audio_enabled))
                if bool(row["audio_enabled"]) != audio_enabled:
                    db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                               (int(user["id"]), "camera_audio_enabled" if audio_enabled else "camera_audio_disabled", "camera", camera_id,
                                json.dumps({"audio_supported": bool(row["audio_supported"])}, separators=(",", ":"))))
            if payload.motion_pre_roll_seconds is not None:
                assignments.append("motion_pre_roll_seconds=?"); values.append(payload.motion_pre_roll_seconds)
            if payload.motion_post_roll_seconds is not None:
                assignments.append("motion_post_roll_seconds=?"); values.append(payload.motion_post_roll_seconds)
            if not enabled:
                assignments.extend(("recording_status='stopped'", "last_error=NULL"))
            elif mode == "motion" and not row["motion_service_url"] and not row["motion_fallback_enabled"]:
                assignments.extend(("recording_status='unsupported'", "last_error='Configure an ONVIF event service or enable server motion fallback'"))
            else:
                assignments.extend(("recording_status='connecting'", "last_error=NULL"))
        values.append(camera_id)
        try:
            db.execute(f"UPDATE cameras SET {','.join(assignments)} WHERE id=?", values)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="A camera with that name already exists") from exc
    if not name_only:
        await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    with connect() as db:
        row = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    return camera_view(row)


@router.post("/api/cameras/{camera_id}/credentials")
async def rotate_camera_credentials(
    camera_id: str,
    payload: CameraCredentialRotateRequest,
    request: Request,
    _: Administrator,
) -> dict[str, object]:
    try:
        stream_url, _ = await _resolve_camera_source(payload)
        safe_stream_url(stream_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with connect() as db:
        camera = db.execute("SELECT id,recording_mode FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    capability = await probe_stream(stream_url)
    if not capability.get("reachable"):
        raise HTTPException(status_code=422, detail=str(capability.get("reason", "Updated camera stream could not be opened")))
    video = capability["video"]
    with connect() as db:
        db.execute(
            "UPDATE cameras SET stream_url_ciphertext=?,live_substream_url_ciphertext=NULL,audio_supported=?,audio_enabled=CASE WHEN ?=0 THEN 0 ELSE audio_enabled END,video_codec=?,video_width=?,video_height=?,last_probe_at=?,last_error=NULL,updated_at=? WHERE id=?",
            (encrypt(stream_url), int(bool(capability.get("audio"))), int(bool(capability.get("audio"))), video["codec"], video["width"], video["height"], datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), camera_id),
        )
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    with connect() as db:
        row = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    return camera_view(row)


@router.post("/api/cameras/{camera_id}/audio/probe")
async def probe_camera_audio(
    camera_id: str,
    request: Request,
    user: Administrator,
) -> dict[str, object]:
    with connect() as db:
        camera = db.execute("SELECT stream_url_ciphertext,audio_enabled FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    try:
        capability = await probe_stream(decrypt(camera["stream_url_ciphertext"]))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Camera stream configuration is unavailable") from exc
    if not capability.get("reachable"):
        raise HTTPException(status_code=503, detail=str(capability.get("reason", "Camera stream could not be opened")))
    supported = bool(capability.get("audio"))
    with connect() as db:
        db.execute("UPDATE cameras SET audio_supported=?,audio_enabled=CASE WHEN ?=0 THEN 0 ELSE audio_enabled END,last_probe_at=?,updated_at=? WHERE id=?",
                   (int(supported), int(supported), datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), camera_id))
        if not supported and camera["audio_enabled"]:
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                       (int(user["id"]), "camera_audio_disabled", "camera", camera_id, '{"reason":"audio_capability_lost"}'))
    if not supported and camera["audio_enabled"]:
        # If the recorder was retaining an audio track, restart it with the safe muted setting.
        await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    return {"audio_supported": supported, "audio_enabled": bool(camera["audio_enabled"] and supported), "audio_status": "available_disabled" if supported and not camera["audio_enabled"] else "enabled" if supported else "unsupported", "detail": "AAC is used in recordings; live listening is served as MP3 for broad browser playback."}


@router.post("/api/cameras/{camera_id}/motion-source")
async def configure_motion_source(
    camera_id: str,
    payload: MotionSourceUpdateRequest,
    request: Request,
    _: Administrator,
) -> dict[str, object]:
    credentials_supplied = payload.username is not None or payload.password is not None
    if credentials_supplied and bool(payload.username) != bool(payload.password):
        raise HTTPException(status_code=422, detail="Provide both ONVIF motion username and password")
    with connect() as db:
        row = db.execute("SELECT recording_mode,recording_enabled,motion_service_url,motion_fallback_enabled,motion_zones_json,motion_sensitivity,motion_pre_roll_seconds,motion_post_roll_seconds FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        service_url = payload.service_url if "service_url" in payload.model_fields_set else row["motion_service_url"]
        fallback_enabled = payload.fallback_enabled if payload.fallback_enabled is not None else bool(row["motion_fallback_enabled"])
        if not service_url and not fallback_enabled:
            raise HTTPException(status_code=422, detail="Configure an ONVIF event service or enable server motion fallback")
        assignments = ["motion_service_url=?", "motion_pre_roll_seconds=?", "motion_post_roll_seconds=?", "motion_source_status=?"]
        pre_roll_seconds = payload.pre_roll_seconds if payload.pre_roll_seconds is not None else row["motion_pre_roll_seconds"]
        post_roll_seconds = payload.post_roll_seconds if payload.post_roll_seconds is not None else row["motion_post_roll_seconds"]
        values: list[object] = [service_url, pre_roll_seconds, post_roll_seconds, "untested" if service_url else "unsupported"]
        if credentials_supplied:
            assignments.append("motion_credentials_ciphertext=?")
            values.append(encrypt(json.dumps({"username": payload.username or "", "password": payload.password or ""}, separators=(",", ":"))))
        zones = payload.zones if payload.zones is not None else [MotionZoneRequest.model_validate(zone) for zone in json.loads(row["motion_zones_json"])]
        sensitivity = payload.sensitivity if payload.sensitivity is not None else row["motion_sensitivity"]
        assignments.extend(("motion_fallback_enabled=?", "motion_zones_json=?", "motion_sensitivity=?", "server_motion_status=?"))
        values.extend((int(fallback_enabled), json.dumps([zone.model_dump() for zone in zones], separators=(",", ":")), sensitivity, "waiting" if fallback_enabled else "disabled"))
        assignments.extend(("recording_status=CASE WHEN recording_enabled=1 AND recording_mode='motion' THEN 'connecting' ELSE recording_status END", "last_error=NULL", "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"))
        values.append(camera_id)
        db.execute(
            f"UPDATE cameras SET {','.join(assignments)} WHERE id=?", values,
        )
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    with connect() as db:
        camera = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    return camera_view(camera)
