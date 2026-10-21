from __future__ import annotations

import asyncio
import csv
import io
import os
import re
import secrets
import shutil
import sqlite3
import uuid
import json
import time
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware

from .security import hash_password, new_session_token, token_digest, verify_password
from .store import connect, database_path, initialize
from .credentials import encrypt, decrypt
from .recording import RecordingManager, masked_stream_url, probe_stream, recordings_path, safe_stream_url, _manifest_bytes
from .health import recording_health
from .discovery import DiscoveryResult, discover_onvif, discover_onvif_ip_range
from .onvif_stream import list_camera_stream_profiles, resolve_camera_stream
from .scheduling import effective_policy, validate_schedule
from . import diagnostics
from .retention import acquire_playback, coverage_summary, effective_retention, release_playback
from .storage import assign_group_volume, configured_volumes, refresh_registry, storage_status
from .preservation import delete_segment as delete_preserved_segment, get_segment_hold, preserve_segment, release_segment_hold
from .exports import ExportManager, create_export_record, export_output_path, get_export, list_exports, request_cancel, verify_evidence_bytes, _job_view, MAX_EXPORT_CAMERAS, MAX_EXPORT_WINDOW, MAX_VERIFY_PACKAGE_BYTES
from .incidents import acknowledge_incident, incident_view, list_incidents, sync_storage_incidents
from .webhooks import EVENT_TYPES as WEBHOOK_EVENT_TYPES, WebhookManager, create_endpoint as create_webhook_endpoint, delete_endpoint as delete_webhook_endpoint, enqueue_test as enqueue_webhook_test, list_deliveries as list_webhook_deliveries, list_endpoints as list_webhook_endpoints, rotate_secret as rotate_webhook_secret, update_endpoint as update_webhook_endpoint
from .heartbeat import HeartbeatManager, configure_heartbeat, disable_heartbeat, heartbeat_status, queue_heartbeat_test, rotate_heartbeat_secret
from .detection import DetectionManager, detector_availability, effective_camera_config, model_options, openvino_provider_options, validate_detector, MODEL_ID, MODEL_RUNTIME, MODEL_VERSION
from .display_settings import format_display_datetime, validate_display_settings

APP_VERSION = "0.1.0"
COOKIE_NAME = "nvr_session"
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.@-]{3,128}$")
CameraBrand = Literal["auto", "acti", "axis", "bosch", "dahua", "hanwha", "hikvision", "reolink", "samsung", "uniview", "other"]
MAX_LIVE_VIEWERS = 16
MAX_LIVE_AUDIO_LISTENERS = 16
_live_view_lock = threading.Lock()
_live_view_count = 0
_live_audio_count = 0
_live_camera_status: dict[str, dict[str, object]] = {}


def _secure_cookie() -> bool:
    if os.environ.get("NVR_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}:
        return True
    return os.environ.get("NVR_PUBLIC_URL", "").lower().startswith("https://")


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.umask(0o077)
    initialize()
    app.state.exports = ExportManager()
    await app.state.exports.start()
    app.state.webhooks = WebhookManager()
    await app.state.webhooks.start()
    app.state.heartbeat = HeartbeatManager()
    await app.state.heartbeat.start()
    app.state.recorders = RecordingManager()
    await app.state.recorders.start()
    app.state.detectors = DetectionManager()
    await app.state.detectors.start()
    yield
    await app.state.detectors.stop()
    await app.state.exports.stop()
    await app.state.webhooks.stop()
    await app.state.heartbeat.stop()
    await app.state.recorders.stop()


app = FastAPI(title="Workplace NVR", version=APP_VERSION, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("NVR_OIDC_SESSION_SECRET") or secrets.token_urlsafe(48),
    same_site="lax",
    https_only=_secure_cookie(),
    session_cookie="nvr_oidc_flow",
    max_age=600,
)
oauth = OAuth()


def _oidc_settings() -> dict[str, str] | None:
    required = ("NVR_PUBLIC_URL", "NVR_OIDC_TENANT_ID", "NVR_OIDC_CLIENT_ID", "NVR_OIDC_CLIENT_SECRET", "NVR_OIDC_SESSION_SECRET")
    values = {key: os.environ.get(key, "").strip() for key in required}
    if not all(values.values()) or len(values["NVR_OIDC_SESSION_SECRET"]) < 32:
        return None
    if not any(os.environ.get(key, "").strip() for key in ("NVR_OIDC_ADMIN_ROLES", "NVR_OIDC_OPERATOR_ROLES")):
        return None
    public_url = values["NVR_PUBLIC_URL"].rstrip("/")
    parsed_url = urlsplit(public_url)
    if (parsed_url.scheme != "https" and not (parsed_url.scheme == "http" and parsed_url.hostname in {"localhost", "127.0.0.1"})) or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.path or parsed_url.query or parsed_url.fragment:
        return None
    try:
        if str(uuid.UUID(values["NVR_OIDC_TENANT_ID"])).casefold() != values["NVR_OIDC_TENANT_ID"].casefold():
            return None
    except ValueError:
        return None
    return {
        "public_url": public_url,
        "tenant_id": values["NVR_OIDC_TENANT_ID"],
        "client_id": values["NVR_OIDC_CLIENT_ID"],
        "client_secret": values["NVR_OIDC_CLIENT_SECRET"],
        "discovery_url": f"https://login.microsoftonline.com/{values['NVR_OIDC_TENANT_ID']}/v2.0/.well-known/openid-configuration",
    }


def _oidc_client():
    config = _oidc_settings()
    if config is None:
        return None
    return oauth.register(
        name="entra",
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        server_metadata_url=config["discovery_url"],
        client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256", "trust_env": False},
    )


def _mapped_role(claims: dict[str, object]) -> str | None:
    roles = claims.get("roles", [])
    if isinstance(roles, str):
        roles = [roles]
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        return None
    admin_roles = {x.strip() for x in os.environ.get("NVR_OIDC_ADMIN_ROLES", "").split(",") if x.strip()}
    operator_roles = {x.strip() for x in os.environ.get("NVR_OIDC_OPERATOR_ROLES", "").split(",") if x.strip()}
    assigned = set(roles)
    if assigned & admin_roles:
        return "administrator"
    if assigned & operator_roles:
        return "operator"
    return None


class BootstrapRequest(BaseModel):
    username: str = Field(min_length=3, max_length=128)
    password: str = Field(min_length=14, max_length=1024)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class DisplaySettingsRequest(BaseModel):
    time_zone: str = Field(min_length=1, max_length=128)
    date_format: str = Field(min_length=1, max_length=32)
    clock_format: str = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_display_options(self) -> "DisplaySettingsRequest":
        try:
            validate_display_settings(self.time_zone.strip(), self.date_format, self.clock_format)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        self.time_zone = self.time_zone.strip()
        return self


class ViewerLayoutRequest(BaseModel):
    camera_ids: list[str] = Field(default_factory=list, max_length=MAX_LIVE_VIEWERS)
    grid_size: int = Field(default=4)
    tile_camera_ids: list[str | None] | None = Field(default=None, max_length=MAX_LIVE_VIEWERS)

    @model_validator(mode="after")
    def validate_layout(self) -> "ViewerLayoutRequest":
        if self.tile_camera_ids is None and self.grid_size == 4 and len(self.camera_ids) > 4:
            self.grid_size = 9 if len(self.camera_ids) <= 9 else 16
        if self.grid_size not in {1, 4, 9, 16}:
            raise ValueError("Grid size must be 1, 4, 9, or 16")
        if self.tile_camera_ids is None:
            self.tile_camera_ids = [*self.camera_ids, *([None] * max(0, self.grid_size - len(self.camera_ids)))]
        if len(self.tile_camera_ids) != self.grid_size:
            raise ValueError("Tile positions must match the selected grid size")
        tile_ids = [camera_id for camera_id in self.tile_camera_ids if camera_id is not None]
        if not tile_ids and not self.camera_ids:
            raise ValueError("A saved view must contain at least one camera")
        if len(set(self.camera_ids)) != len(self.camera_ids):
            raise ValueError("A camera can appear only once in a layout")
        if len(set(tile_ids)) != len(tile_ids):
            raise ValueError("A camera can appear only once in a layout")
        if self.camera_ids and set(self.camera_ids) != set(tile_ids):
            raise ValueError("Camera selection must match the populated tile positions")
        self.camera_ids = tile_ids
        return self


class LiveWorkspaceRequest(BaseModel):
    camera_ids: list[str] = Field(default_factory=list, max_length=MAX_LIVE_VIEWERS)
    grid_size: int = Field(default=4)
    tile_camera_ids: list[str | None] = Field(default_factory=list, max_length=MAX_LIVE_VIEWERS)
    sidebar_collapsed: bool = False

    @model_validator(mode="after")
    def validate_workspace(self) -> "LiveWorkspaceRequest":
        if not self.tile_camera_ids and self.grid_size == 4 and len(self.camera_ids) > 4:
            self.grid_size = 9 if len(self.camera_ids) <= 9 else 16
        if self.grid_size not in {1, 4, 9, 16}:
            raise ValueError("Grid size must be 1, 4, 9, or 16")
        if self.tile_camera_ids and len(self.tile_camera_ids) != self.grid_size:
            raise ValueError("Tile positions must match the selected grid size")
        tile_ids = [camera_id for camera_id in self.tile_camera_ids if camera_id is not None]
        if len(set(self.camera_ids)) != len(self.camera_ids):
            raise ValueError("A camera can appear only once in a workspace")
        if len(set(tile_ids)) != len(tile_ids):
            raise ValueError("A camera can appear only once in a workspace")
        if self.camera_ids and set(self.camera_ids) != set(tile_ids):
            raise ValueError("Camera selection must match the populated tile positions")
        if not self.tile_camera_ids:
            self.tile_camera_ids = [*self.camera_ids, *([None] * max(0, self.grid_size - len(self.camera_ids)))]
        if len(self.tile_camera_ids) != self.grid_size:
            raise ValueError("Workspace cameras must fit the selected grid size")
        self.camera_ids = tile_ids
        return self


class CameraProbeRequest(BaseModel):
    stream_url: str | None = Field(default=None, min_length=8, max_length=2048)
    camera_ip: str | None = Field(default=None, min_length=7, max_length=45)
    camera_username: str | None = Field(default=None, max_length=256)
    camera_password: str | None = Field(default=None, max_length=1024)
    camera_brand: CameraBrand = "auto"
    onvif_profile_token: str | None = Field(default=None, max_length=256)
    live_substream_profile_token: str | None = Field(default=None, max_length=256)
    device_service_url: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_source(self) -> "CameraProbeRequest":
        if bool(self.stream_url) == bool(self.camera_ip):
            raise ValueError("Provide either a camera IP address or an RTSP stream URL")
        if bool(self.camera_username) != bool(self.camera_password):
            raise ValueError("Provide both camera username and password, or leave both blank")
        if self.live_substream_profile_token and (not self.camera_ip or not self.onvif_profile_token):
            raise ValueError("A live substream requires an ONVIF camera address and a selected main profile")
        if self.live_substream_profile_token and self.live_substream_profile_token == self.onvif_profile_token:
            raise ValueError("Choose a different profile for the optional live substream")
        return self


class SynchronizedPlaybackRequest(BaseModel):
    camera_ids: list[str] = Field(min_length=1, max_length=8)
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

    @model_validator(mode="after")
    def validate_interval(self) -> "ExportRequest":
        if len(set(self.camera_ids)) != len(self.camera_ids):
            raise ValueError("Choose each camera only once")
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


class CameraImportRequest(BaseModel):
    csv_text: str = Field(min_length=1, max_length=1_000_000)


class CameraImportRow(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    stream_url: str = Field(min_length=8, max_length=2048)
    recording_mode: Literal["motion", "continuous", "disabled"] = "motion"
    group_name: str | None = Field(default=None, max_length=80)
    onvif_uuid: str | None = None

    @field_validator("name", "group_name")
    @classmethod
    def trim_names(cls, value: str | None) -> str | None:
        value = value.strip() if isinstance(value, str) else value
        return (value or None) if isinstance(value, str) else value

    @field_validator("stream_url")
    @classmethod
    def trim_stream_url(cls, value: str) -> str:
        return value.strip()

    @field_validator("onvif_uuid")
    @classmethod
    def validate_onvif_uuid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ValueError("ONVIF identity must be a UUID") from exc


class CameraBulkUpdateRequest(BaseModel):
    camera_ids: list[str] = Field(min_length=1, max_length=500)
    group_name: str | None = Field(default=None, max_length=80)
    recording_mode: Literal["motion", "continuous", "disabled"] | None = None
    recording_enabled: bool | None = None

    @field_validator("group_name")
    @classmethod
    def trim_group_name(cls, value: str | None) -> str | None:
        value = value.strip() if isinstance(value, str) else value
        return (value or None) if isinstance(value, str) else value


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
    recording_mode: Literal["motion", "continuous", "disabled"] | None = None
    recording_enabled: bool | None = None
    motion_pre_roll_seconds: int | None = Field(default=None, ge=0, le=120)
    motion_post_roll_seconds: int | None = Field(default=None, ge=1, le=600)
    audio_enabled: bool | None = None

    @field_validator("recording_mode")
    @classmethod
    def require_update_fields(cls, value: str | None) -> str | None:
        return value


class AIDetectionConfigRequest(BaseModel):
    enabled: bool = False
    analysis_fps: float = Field(default=0.5, ge=0.1, le=5)
    confidence_threshold: float = Field(default=0.55, ge=0.1, le=0.99)
    use_substream: bool = True
    zones: list[MotionZoneRequest] = Field(default_factory=list, max_length=16)
    inherit_group: bool = False


class AIRuntimeSettingsRequest(BaseModel):
    max_active_cameras: int = Field(ge=1, le=16)
    provider: Literal["CPU", "GPU"] | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=128)


class AIRuntimeValidationRequest(BaseModel):
    provider: Literal["CPU", "GPU"]
    model_id: str = Field(min_length=1, max_length=128)


class CameraGroupCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def trim_group_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Group name is required")
        return value


class CameraGroupUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def trim_group_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Group name is required")
        return value


class CameraGroupMembershipRequest(BaseModel):
    camera_ids: list[str] = Field(max_length=5000)


class RetentionRequest(BaseModel):
    retention_days: int = Field(ge=1, le=3650)


class StorageAssignmentRequest(BaseModel):
    volume_id: str | None = Field(default=None, max_length=64)


class HoldRequest(BaseModel):
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class WeeklyScheduleInterval(BaseModel):
    days: list[Annotated[int, Field(ge=0, le=6)]] = Field(min_length=1, max_length=7)
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    mode: Literal["motion", "continuous", "disabled"]


class GroupScheduleRequest(BaseModel):
    timezone: str = Field(min_length=1, max_length=128)
    intervals: list[WeeklyScheduleInterval] = Field(max_length=200)

    @model_validator(mode="after")
    def validate_intervals(self) -> "GroupScheduleRequest":
        try:
            validate_schedule(self.timezone, [interval.model_dump() for interval in self.intervals])
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


class CameraScheduleRequest(GroupScheduleRequest):
    timezone: str = "UTC"
    intervals: list[WeeklyScheduleInterval] = Field(default_factory=list, max_length=200)
    inherit_group: bool = False

    @model_validator(mode="after")
    def require_policy_or_inheritance(self) -> "CameraScheduleRequest":
        if not self.inherit_group and not self.timezone.strip():
            raise ValueError("Choose a timezone or inherit the assigned camera group's schedule")
        return self


class DiscoveryScanRequest(BaseModel):
    timeout_seconds: float = Field(default=5, ge=0.5, le=15)
    ip_range: str | None = Field(default=None, min_length=1, max_length=64)


class DiagnosticCaptureRequest(BaseModel):
    enabled: bool
    hours: int = Field(default=4, ge=1, le=24)


class DiscoveredCameraAddRequest(CameraProbeRequest):
    name: str = Field(min_length=1, max_length=120)
    recording_mode: Literal["motion", "continuous", "disabled"] = "motion"
    recording_enabled: bool = False
    motion_username: str | None = Field(default=None, max_length=256)
    motion_password: str | None = Field(default=None, max_length=1024)
    motion_pre_roll_seconds: int = Field(default=10, ge=0, le=120)
    motion_post_roll_seconds: int = Field(default=30, ge=1, le=600)
    motion_fallback_enabled: bool = False
    motion_zones: list[MotionZoneRequest] = Field(default_factory=list, max_length=16)
    motion_sensitivity: int = Field(default=50, ge=1, le=100)


class DiscoveredCameraLinkRequest(BaseModel):
    camera_id: str = Field(min_length=1, max_length=64)


def _session_hours() -> int:
    try:
        return min(max(int(os.environ.get("NVR_SESSION_HOURS", "12")), 1), 168)
    except ValueError:
        return 12


def _new_session(response: Response, user_id: int) -> None:
    raw = new_session_token()
    session_id = str(uuid.uuid4())
    expires = datetime.now(timezone.utc) + timedelta(hours=_session_hours())
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (datetime.now(timezone.utc).isoformat(),))
        db.execute(
            "INSERT INTO sessions(session_id, token_hash, user_id, expires_at) VALUES (?, ?, ?, ?)",
            (session_id, token_digest(raw), user_id, expires.isoformat()),
        )
    response.set_cookie(
        COOKIE_NAME,
        raw,
        httponly=True,
        secure=_secure_cookie(),
        samesite="strict",
        path="/",
        max_age=_session_hours() * 3600,
    )


def current_user(request: Request) -> sqlite3.Row:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    with connect() as db:
        row = db.execute(
            "SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>?",
            (token_digest(raw), datetime.now(timezone.utc).isoformat()),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    return row


def administrator(user: Annotated[sqlite3.Row, Depends(current_user)]) -> sqlite3.Row:
    if user["role"] != "administrator":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator role required")
    return user


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(Path(__file__).with_name("static") / "index.html")


@app.get("/app.js", include_in_schema=False)
def javascript() -> FileResponse:
    return FileResponse(Path(__file__).with_name("static") / "app.js", media_type="text/javascript")


@app.get("/healthz", include_in_schema=False)
def health() -> dict[str, str]:
    try:
        with connect() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok"}
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


@app.get("/api/setup/status")
def setup_status() -> dict[str, bool]:
    with connect() as db:
        configured = db.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0] == 1
    return {"setup_required": not configured}


@app.post("/api/setup/admin", status_code=status.HTTP_201_CREATED)
def bootstrap(payload: BootstrapRequest, response: Response) -> dict[str, str]:
    username = payload.username.strip()
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(status_code=422, detail="Use 3–128 letters, numbers, or . _ @ - characters")
    if len(payload.password) < 14 or not any(c.isalpha() for c in payload.password) or not any(c.isdigit() for c in payload.password):
        raise HTTPException(status_code=422, detail="Password must be at least 14 characters and include a letter and a number")
    with connect() as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0]:
                db.rollback()
                raise HTTPException(status_code=409, detail="Initial setup is already complete")
            cur = db.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (?, ?, 'administrator')",
                (username, hash_password(payload.password)),
            )
            user_id = int(cur.lastrowid)
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="Initial setup is already complete") from exc
    _new_session(response, user_id)
    return {"username": username, "role": "administrator"}


@app.post("/api/auth/login")
def login(payload: LoginRequest, response: Response) -> dict[str, str]:
    with connect() as db:
        user = db.execute(
            "SELECT id, username, password_hash, role FROM users WHERE username=? COLLATE NOCASE",
            (payload.username.strip(),),
        ).fetchone()
    if user is None or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _new_session(response, user["id"])
    return {"username": user["username"], "role": user["role"]}


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response) -> Response:
    raw = request.cookies.get(COOKIE_NAME)
    if raw:
        with connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(raw),))
    request.session.clear()
    response.delete_cookie(COOKIE_NAME, path="/", secure=_secure_cookie(), httponly=True, samesite="strict")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@app.get("/api/auth/me")
def me(user: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, str]:
    return {"username": user["username"], "role": user["role"]}


def _display_settings_view(row: sqlite3.Row) -> dict[str, object]:
    return {"time_zone": row["time_zone"], "date_format": row["date_format"],
            "clock_format": row["clock_format"], "updated_at": row["updated_at"]}


@app.get("/api/settings/display")
def get_display_settings(_: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    with connect() as db:
        row = db.execute("SELECT time_zone,date_format,clock_format,updated_at FROM site_display_settings WHERE singleton_id=1").fetchone()
    return _display_settings_view(row)


@app.post("/api/settings/display/preview")
def preview_display_settings(payload: DisplaySettingsRequest, _: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    sample = datetime(2026, 10, 21, 16, 5, 6, tzinfo=timezone.utc)
    return {**payload.model_dump(), "preview": format_display_datetime(
        sample, payload.time_zone, payload.date_format, payload.clock_format
    )}


@app.put("/api/settings/display")
def save_display_settings(payload: DisplaySettingsRequest, _: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    updated_at = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute(
            "UPDATE site_display_settings SET time_zone=?,date_format=?,clock_format=?,updated_at=? WHERE singleton_id=1",
            (payload.time_zone, payload.date_format, payload.clock_format, updated_at),
        )
        row = db.execute("SELECT time_zone,date_format,clock_format,updated_at FROM site_display_settings WHERE singleton_id=1").fetchone()
    return _display_settings_view(row)


def _load_live_views(user: sqlite3.Row) -> dict[str, object]:
    with connect() as db:
        rows = db.execute(
            "SELECT name,camera_ids_json,grid_size,tile_camera_ids_json FROM viewer_layouts WHERE user_id=? ORDER BY name COLLATE NOCASE",
            (int(user["id"]),),
        ).fetchall()
        shared_rows = db.execute(
            "SELECT name,camera_ids_json,grid_size,tile_camera_ids_json FROM shared_viewer_layouts ORDER BY name COLLATE NOCASE"
        ).fetchall()
    def decode_layout(row: sqlite3.Row, scope: str) -> dict[str, object] | None:
        try:
            camera_ids = json.loads(row["camera_ids_json"])
            tile_camera_ids = json.loads(row["tile_camera_ids_json"] or "null")
            if not isinstance(camera_ids, list):
                return None
            if not isinstance(tile_camera_ids, list):
                tile_camera_ids = [*camera_ids, *([None] * max(0, int(row["grid_size"]) - len(camera_ids)))]
            return {"name": row["name"], "camera_ids": camera_ids, "grid_size": int(row["grid_size"]),
                    "tile_camera_ids": tile_camera_ids, "scope": scope}
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    layouts = []
    for row in rows:
        layout = decode_layout(row, "personal")
        if layout is not None:
            layouts.append(layout)
    shared_views = [layout for row in shared_rows if (layout := decode_layout(row, "shared")) is not None]
    return {"layouts": layouts, "shared_views": shared_views}


@app.get("/api/live/layouts")
def get_viewer_layouts(user: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    views = _load_live_views(user)
    with connect() as db:
        available = {row["id"] for row in db.execute("SELECT id FROM cameras")}
    layouts = [{"name": layout["name"], "camera_ids": [camera_id for camera_id in layout["camera_ids"] if camera_id in available]}
               for layout in views["layouts"] if any(camera_id in available for camera_id in layout["camera_ids"])]
    return {"layouts": layouts}


@app.get("/api/live/views")
def get_live_views(user: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    return _load_live_views(user)


@app.put("/api/live/layouts/{name}")
def save_viewer_layout(
    name: str,
    payload: ViewerLayoutRequest,
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, object]:
    name = name.strip()
    if not name or len(name) > 60:
        raise HTTPException(status_code=422, detail="Layout name must be between 1 and 60 characters")
    with connect() as db:
        present = {row["id"] for row in db.execute("SELECT id FROM cameras").fetchall()}
        if not set(payload.camera_ids) <= present:
            raise HTTPException(status_code=422, detail="Layout contains an unknown camera")
        db.execute(
            "INSERT INTO viewer_layouts(user_id,name,camera_ids_json,updated_at,grid_size,tile_camera_ids_json) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(user_id,name) DO UPDATE SET camera_ids_json=excluded.camera_ids_json,updated_at=excluded.updated_at,"
            "grid_size=excluded.grid_size,tile_camera_ids_json=excluded.tile_camera_ids_json",
            (int(user["id"]), name, json.dumps(payload.camera_ids), datetime.now(timezone.utc).isoformat(),
             payload.grid_size, json.dumps(payload.tile_camera_ids)),
        )
    return {"name": name, "camera_ids": payload.camera_ids}


@app.put("/api/live/shared-layouts/{name}")
def save_shared_viewer_layout(
    name: str, payload: ViewerLayoutRequest, user: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    name = name.strip()
    if not name or len(name) > 60:
        raise HTTPException(status_code=422, detail="Layout name must be between 1 and 60 characters")
    with connect() as db:
        present = {row["id"] for row in db.execute("SELECT id FROM cameras").fetchall()}
        if not set(payload.camera_ids) <= present:
            raise HTTPException(status_code=422, detail="Layout contains an unknown camera")
        db.execute(
            "INSERT INTO shared_viewer_layouts(name,camera_ids_json,grid_size,tile_camera_ids_json,created_by,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET camera_ids_json=excluded.camera_ids_json,"
            "grid_size=excluded.grid_size,tile_camera_ids_json=excluded.tile_camera_ids_json,created_by=excluded.created_by,"
            "updated_at=excluded.updated_at",
            (name, json.dumps(payload.camera_ids), payload.grid_size, json.dumps(payload.tile_camera_ids),
             int(user["id"]), datetime.now(timezone.utc).isoformat()),
        )
    return {"name": name, "camera_ids": payload.camera_ids, "grid_size": payload.grid_size,
            "tile_camera_ids": payload.tile_camera_ids, "scope": "shared"}


@app.delete("/api/live/shared-layouts/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shared_viewer_layout(name: str, user: Annotated[sqlite3.Row, Depends(administrator)]) -> Response:
    with connect() as db:
        db.execute("DELETE FROM shared_viewer_layouts WHERE name=? COLLATE NOCASE", (name.strip(),))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/live/workspace")
def get_live_workspace(user: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    with connect() as db:
        row = db.execute("SELECT * FROM live_workspace_states WHERE user_id=?", (int(user["id"]),)).fetchone()
    if row is None:
        return {"camera_ids": None, "tile_camera_ids": [], "grid_size": 4, "sidebar_collapsed": False}
    try:
        camera_ids = json.loads(row["camera_ids_json"])
        tile_camera_ids = json.loads(row["tile_camera_ids_json"])
    except (TypeError, json.JSONDecodeError):
        camera_ids, tile_camera_ids = [], []
    return {"camera_ids": camera_ids, "tile_camera_ids": tile_camera_ids, "grid_size": row["grid_size"],
            "sidebar_collapsed": bool(row["sidebar_collapsed"])}


@app.put("/api/live/workspace")
def save_live_workspace(payload: LiveWorkspaceRequest, user: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    with connect() as db:
        present = {row["id"] for row in db.execute("SELECT id FROM cameras").fetchall()}
        if not set(payload.camera_ids) <= present:
            raise HTTPException(status_code=422, detail="Workspace contains an unknown camera")
        db.execute(
            "INSERT INTO live_workspace_states(user_id,camera_ids_json,tile_camera_ids_json,grid_size,sidebar_collapsed,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET camera_ids_json=excluded.camera_ids_json,"
            "tile_camera_ids_json=excluded.tile_camera_ids_json,grid_size=excluded.grid_size,"
            "sidebar_collapsed=excluded.sidebar_collapsed,updated_at=excluded.updated_at",
            (int(user["id"]), json.dumps(payload.camera_ids), json.dumps(payload.tile_camera_ids), payload.grid_size,
             int(payload.sidebar_collapsed), datetime.now(timezone.utc).isoformat()),
        )
    return {"camera_ids": payload.camera_ids, "tile_camera_ids": payload.tile_camera_ids,
            "grid_size": payload.grid_size, "sidebar_collapsed": payload.sidebar_collapsed}


@app.delete("/api/live/layouts/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_viewer_layout(name: str, user: Annotated[sqlite3.Row, Depends(current_user)]) -> Response:
    with connect() as db:
        db.execute("DELETE FROM viewer_layouts WHERE user_id=? AND name=?", (int(user["id"]), name))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
def live_view_status(camera_id: str, _: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
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
    _: Annotated[sqlite3.Row, Depends(current_user)],
) -> StreamingResponse:
    with connect() as db:
        camera = db.execute("SELECT stream_url_ciphertext,audio_supported,audio_enabled FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
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
    _: Annotated[sqlite3.Row, Depends(current_user)],
    quality: Literal["low", "high"] = Query(default="low"),
) -> StreamingResponse:
    with connect() as db:
        camera = db.execute("SELECT stream_url_ciphertext,live_substream_url_ciphertext FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
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


@app.get("/api/auth/oidc/status")
def oidc_status() -> dict[str, bool]:
    return {"enabled": _oidc_settings() is not None}


@app.get("/api/auth/oidc/login", include_in_schema=False, response_model=None)
async def oidc_login(request: Request) -> dict[str, str] | RedirectResponse:
    config = _oidc_settings()
    client = _oidc_client()
    if config is None or client is None:
        raise HTTPException(status_code=503, detail="Entra sign-in is not configured; use the emergency local administrator account")
    try:
        redirect_uri = config["public_url"] + "/api/auth/oidc/callback"
        if request.query_params.get("format") == "json":
            authorize_data = await client.create_authorization_url(redirect_uri)
            await client.save_authorize_data(request, redirect_uri=redirect_uri, **authorize_data)
            return {"authorization_url": authorize_data["url"]}
        return await client.authorize_redirect(request, redirect_uri)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Entra sign-in is temporarily unavailable; local administrator sign-in remains available") from exc


@app.get("/api/auth/oidc/callback", include_in_schema=False)
async def oidc_callback(request: Request) -> RedirectResponse:
    config = _oidc_settings()
    client = _oidc_client()
    if config is None or client is None:
        raise HTTPException(status_code=503, detail="Entra sign-in is not configured; use the emergency local administrator account")
    if request.query_params.get("error"):
        raise HTTPException(status_code=503, detail="Entra sign-in did not complete; local administrator sign-in remains available")
    try:
        token = await client.authorize_access_token(request)
        claims = token.get("userinfo")
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Entra sign-in could not be validated") from exc
    if not isinstance(claims, dict) or not isinstance(claims.get("sub"), str) or not claims.get("sub"):
        raise HTTPException(status_code=401, detail="Entra sign-in did not include a valid identity")
    tenant_id = config["tenant_id"].casefold()
    if str(claims.get("tid", "")).casefold() != tenant_id:
        raise HTTPException(status_code=403, detail="This Entra tenant is not allowed")
    role = _mapped_role(claims)
    if role is None:
        raise HTTPException(status_code=403, detail="Your Entra account has no NVR role assignment")
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or not issuer.startswith("https://login.microsoftonline.com/"):
        raise HTTPException(status_code=401, detail="Entra sign-in issuer is invalid")
    subject = claims["sub"]
    display = claims.get("preferred_username")
    username = display.strip()[:128] if isinstance(display, str) and display.strip() else f"entra-{subject[:112]}"
    with connect() as db:
        user = db.execute("SELECT id, username FROM users WHERE provider_issuer=? AND provider_subject=?", (issuer, subject)).fetchone()
        if user is None:
            if db.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone():
                username = f"entra-{subject[:112]}"
            try:
                cur = db.execute(
                    "INSERT INTO users(username,password_hash,role,provider_issuer,provider_subject) VALUES (?,?,?,?,?)",
                    (username, hash_password(secrets.token_urlsafe(48)), role, issuer, subject),
                )
                user_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=403, detail="Unable to provision this Entra identity") from exc
        else:
            user_id = int(user["id"])
            db.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
    redirect = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _new_session(redirect, user_id)
    return redirect


@app.get("/api/admin/sessions")
def list_sessions(_: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    with connect() as db:
        rows = db.execute(
            "SELECT s.session_id,s.created_at,s.expires_at,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.expires_at>? ORDER BY s.created_at DESC", (datetime.now(timezone.utc).isoformat(),)
        ).fetchall()
    return {"sessions": [dict(row) for row in rows]}


@app.delete("/api/admin/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def terminate_session(session_id: str, _: Annotated[sqlite3.Row, Depends(administrator)]) -> Response:
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/system/status")
def system_status(user: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
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
def diagnostics_status(_: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    return diagnostics.capture_status()


@app.post("/api/diagnostics/capture")
def diagnostics_capture(
    payload: DiagnosticCaptureRequest,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    return diagnostics.set_capture(payload.enabled, payload.hours)


@app.get("/api/diagnostics/bundle")
def diagnostics_bundle(_: Annotated[sqlite3.Row, Depends(administrator)]) -> Response:
    return Response(
        content=json.dumps(diagnostics.bundle(), indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="nvr-diagnostics.json"', "Cache-Control": "no-store"},
    )


@app.post("/api/cameras/onvif-profiles")
async def camera_onvif_profiles(
    payload: CameraProfileRequest,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    try:
        service_url, profiles = await list_camera_stream_profiles(
            payload.camera_ip, payload.camera_username or "", payload.camera_password or "",
            payload.device_service_url, payload.camera_brand,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"device_service_url": service_url, "profiles": [profile.__dict__ for profile in profiles]}


def _camera_view(row: sqlite3.Row) -> dict[str, object]:
    try:
        visible_url = masked_stream_url(decrypt(row["stream_url_ciphertext"]))
    except RuntimeError:
        visible_url = "rtsp://credentials-unavailable"
    return {
        "id": row["id"],
        "name": row["name"],
        "group_name": row["group_name"] if "group_name" in row.keys() else None,
        "storage_volume_id": row["storage_volume_id"] if "storage_volume_id" in row.keys() else None,
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


def _insert_camera(
    db: sqlite3.Connection,
    *,
    name: str,
    stream_url: str,
    recording_mode: str,
    capability: dict[str, object],
    onvif_uuid: str | None = None,
    group_id: str | None = None,
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
        "INSERT INTO cameras(id,name,onvif_uuid,group_id,stream_url_ciphertext,live_substream_url_ciphertext,audio_supported,audio_enabled,recording_mode,recording_enabled,recording_status," 
        "motion_service_url,motion_credentials_ciphertext,motion_pre_roll_seconds,motion_post_roll_seconds,motion_source_status," 
        "motion_fallback_enabled,motion_zones_json,motion_sensitivity,server_motion_status,last_error," 
        "video_codec,video_width,video_height,last_probe_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (camera_id, name, onvif_uuid, group_id, encrypt(stream_url.strip()), encrypt(live_substream_url.strip()) if live_substream_url else None, int(bool(capability.get("audio"))), 0, recording_mode, int(recording_enabled), status,
         motion_service_url, credentials, motion_pre_roll_seconds, motion_post_roll_seconds,
         "untested" if motion_service_url else "unsupported", int(motion_fallback_enabled),
         json.dumps([zone.model_dump() for zone in (motion_zones or [])], separators=(",", ":")), motion_sensitivity,
         "waiting" if motion_fallback_enabled else "disabled", error,
         video.get("codec"), video.get("width"), video.get("height")),
    )
    return camera_id


def _record_discovery_probe(db: sqlite3.Connection, device_uuid: str | None, capability: dict[str, object]) -> None:
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


def _record_discovery_failure(db: sqlite3.Connection, device_uuid: str) -> None:
    row = db.execute("SELECT id,capabilities FROM discovery_candidates WHERE device_uuid=?", (device_uuid,)).fetchone()
    if row is None:
        return
    states = json.loads(row["capabilities"])
    states["video_stream"] = "unavailable"
    db.execute("UPDATE discovery_candidates SET capabilities=? WHERE id=?", (json.dumps(states), row["id"]))


@app.post("/api/cameras/test")
async def test_camera_stream(
    payload: CameraProbeRequest,
    _: Annotated[sqlite3.Row, Depends(administrator)],
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
        return payload.stream_url.strip(), service_url
    try:
        source = await resolve_camera_stream(
            payload.camera_ip or "", payload.camera_username or "", payload.camera_password or "",
            service_url or payload.device_service_url, payload.onvif_profile_token, payload.camera_brand,
        )
        return source.stream_url, source.device_service_url
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/cameras")
def list_cameras(_: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    with connect() as db:
        rows = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id ORDER BY c.name COLLATE NOCASE").fetchall()
        cameras = []
        for row in rows:
            camera = _camera_view(row)
            schedule = _effective_schedule_view(db, row["id"])
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


@app.get("/api/camera-groups")
def list_camera_groups(_: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    with connect() as db:
        groups = db.execute("SELECT id,name FROM camera_groups ORDER BY name COLLATE NOCASE").fetchall()
        return {"groups": [_camera_group_view(db, group) for group in groups]}


def _camera_group_view(db: sqlite3.Connection, group: sqlite3.Row) -> dict[str, object]:
    members = db.execute(
        "SELECT id FROM cameras WHERE group_id=? "
        "UNION SELECT camera_id FROM camera_group_memberships WHERE group_id=?",
        (group["id"], group["id"]),
    ).fetchall()
    camera_ids = [row["id"] for row in members]
    return {"id": group["id"], "name": group["name"], "camera_ids": camera_ids, "camera_count": len(camera_ids)}


@app.get("/api/camera-groups/{group_id}/members")
def list_camera_group_members(
    group_id: str,
    _: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, object]:
    with connect() as db:
        group = db.execute("SELECT id,name FROM camera_groups WHERE id=?", (group_id,)).fetchone()
        if group is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        return _camera_group_view(db, group)


@app.post("/api/camera-groups", status_code=status.HTTP_201_CREATED)
def create_camera_group(
    payload: CameraGroupCreateRequest,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, str]:
    name = payload.name
    group_id = str(uuid.uuid4())
    with connect() as db:
        try:
            db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)", (group_id, name))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="A camera group with that name already exists") from exc
    return {"id": group_id, "name": name}


@app.patch("/api/camera-groups/{group_id}")
def update_camera_group(
    group_id: str,
    payload: CameraGroupUpdateRequest,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        try:
            db.execute("UPDATE camera_groups SET name=? WHERE id=?", (payload.name, group_id))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="A camera group with that name already exists") from exc
        group = db.execute("SELECT id,name FROM camera_groups WHERE id=?", (group_id,)).fetchone()
        return _camera_group_view(db, group)


@app.delete("/api/camera-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_camera_group(
    group_id: str,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> Response:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        if db.execute("SELECT 1 FROM cameras WHERE group_id=? LIMIT 1", (group_id,)).fetchone() is not None:
            raise HTTPException(status_code=409, detail="Reassign cameras from this policy group before deleting it")
        db.execute("DELETE FROM recording_policy_overrides WHERE scope_type='group' AND scope_id=?", (group_id,))
        db.execute("DELETE FROM retention_policy_overrides WHERE scope_type='group' AND scope_id=?", (group_id,))
        db.execute("DELETE FROM camera_groups WHERE id=?", (group_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.put("/api/camera-groups/{group_id}/members")
def replace_camera_group_members(
    group_id: str,
    payload: CameraGroupMembershipRequest,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    camera_ids = list(dict.fromkeys(payload.camera_ids))
    with connect() as db:
        group = db.execute("SELECT id,name FROM camera_groups WHERE id=?", (group_id,)).fetchone()
        if group is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        if camera_ids:
            placeholders = ",".join("?" for _ in camera_ids)
            found = db.execute(f"SELECT id FROM cameras WHERE id IN ({placeholders})", camera_ids).fetchall()
            if len(found) != len(camera_ids):
                raise HTTPException(status_code=422, detail="One or more selected cameras do not exist")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM camera_group_memberships WHERE group_id=?", (group_id,))
        db.executemany(
            "INSERT INTO camera_group_memberships(group_id,camera_id) VALUES(?,?)",
            [(group_id, camera_id) for camera_id in camera_ids],
        )
        return _camera_group_view(db, group)


@app.get("/api/camera-groups/{group_id}/retention")
def get_group_retention(group_id: str, _: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        row = db.execute("SELECT retention_days,updated_at FROM retention_policy_overrides WHERE scope_type='group' AND scope_id=?", (group_id,)).fetchone()
    return {"group_id": group_id, "configured": row is not None, "retention_days": int(row["retention_days"]) if row else 30, "policy_source": "group" if row else "default", "updated_at": row["updated_at"] if row else None}


@app.put("/api/camera-groups/{group_id}/retention")
def set_group_retention(group_id: str, payload: RetentionRequest, _: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('group',?,?) ON CONFLICT(scope_type,scope_id) DO UPDATE SET retention_days=excluded.retention_days,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')", (group_id,payload.retention_days))
    return get_group_retention(group_id, _=None)


@app.delete("/api/camera-groups/{group_id}/retention", status_code=status.HTTP_204_NO_CONTENT)
def delete_group_retention(group_id: str, _: Annotated[sqlite3.Row, Depends(administrator)]) -> Response:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        db.execute("DELETE FROM retention_policy_overrides WHERE scope_type='group' AND scope_id=?", (group_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/cameras/{camera_id}/retention")
def get_camera_retention(camera_id: str, _: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        days, source = effective_retention(db, camera_id)
        summary = coverage_summary(db, camera_id)
        local = db.execute("SELECT 1 FROM retention_policy_overrides WHERE scope_type='camera' AND scope_id=?", (camera_id,)).fetchone()
    return {"camera_id": camera_id, "inherits_group": local is None, **summary, "policy_source": source}


@app.put("/api/cameras/{camera_id}/retention")
def set_camera_retention(camera_id: str, payload: RetentionRequest, _: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,?) ON CONFLICT(scope_type,scope_id) DO UPDATE SET retention_days=excluded.retention_days,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')", (camera_id,payload.retention_days))
    return get_camera_retention(camera_id, _=None)


@app.delete("/api/cameras/{camera_id}/retention", status_code=status.HTTP_204_NO_CONTENT)
def delete_camera_retention(camera_id: str, _: Annotated[sqlite3.Row, Depends(administrator)]) -> Response:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        db.execute("DELETE FROM retention_policy_overrides WHERE scope_type='camera' AND scope_id=?", (camera_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/storage")
def get_storage_status(_: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    refresh_registry()
    result = storage_status()
    with connect() as db:
        sync_storage_incidents(db, result["volumes"])
    return result


@app.get("/api/camera-groups/{group_id}/storage-volume")
def get_group_storage_volume(group_id: str, _: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    with connect() as db:
        row=db.execute("SELECT id,name,storage_volume_id FROM camera_groups WHERE id=?",(group_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404,detail="Camera group not found")
    return {"group_id":group_id,"volume_id":row["storage_volume_id"] or "default","configured_volumes":list(configured_volumes())}


@app.put("/api/camera-groups/{group_id}/storage-volume")
async def set_group_storage_volume(group_id: str, payload: StorageAssignmentRequest, request: Request, _: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    try:
        assign_group_volume(group_id,payload.volume_id)
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc
    with connect() as db:
        camera_ids=[row["id"] for row in db.execute("SELECT id FROM cameras WHERE group_id=?",(group_id,)).fetchall()]
    await asyncio.gather(*(request.app.state.recorders.sync_camera(camera_id,force_restart=True) for camera_id in camera_ids))
    return get_group_storage_volume(group_id,_=None)


def _schedule_document(timezone_name: str, intervals: list[dict[str, object]]) -> dict[str, object]:
    return {"timezone": timezone_name, "intervals": intervals}


def _effective_schedule_view(db: sqlite3.Connection, camera_id: str) -> dict[str, object]:
    policy = effective_policy(db, camera_id, datetime.now(timezone.utc))
    return {
        "effective_recording_mode": policy["mode"],
        "recording_policy_source": policy["source"],
        "schedule_timezone": policy["timezone"],
    }


@app.get("/api/camera-groups/{group_id}/schedule")
def get_group_schedule(
    group_id: str,
    _: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        row = db.execute("SELECT timezone_name,schedule_json,updated_at FROM recording_policy_overrides WHERE scope_type='group' AND scope_id=?", (group_id,)).fetchone()
    if row is None:
        return {"group_id": group_id, "configured": False, "timezone": "UTC", "intervals": []}
    return {"group_id": group_id, "configured": True, "timezone": row["timezone_name"], "intervals": json.loads(row["schedule_json"]), "updated_at": row["updated_at"]}


@app.put("/api/camera-groups/{group_id}/schedule")
async def update_group_schedule(
    group_id: str,
    payload: GroupScheduleRequest,
    request: Request,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    intervals = validate_schedule(payload.timezone, [item.model_dump() for item in payload.intervals])
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        db.execute(
            "INSERT INTO recording_policy_overrides(scope_type,scope_id,timezone_name,schedule_json) VALUES('group',?,?,?) "
            "ON CONFLICT(scope_type,scope_id) DO UPDATE SET timezone_name=excluded.timezone_name,schedule_json=excluded.schedule_json,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (group_id, payload.timezone, json.dumps(intervals, separators=(",", ":"))),
        )
        camera_ids = [row["id"] for row in db.execute("SELECT id FROM cameras WHERE group_id=?", (group_id,)).fetchall()]
    await asyncio.gather(*(request.app.state.recorders.sync_camera(camera_id, force_restart=True) for camera_id in camera_ids))
    return {"group_id": group_id, "configured": True, **_schedule_document(payload.timezone, intervals)}


@app.delete("/api/camera-groups/{group_id}/schedule", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group_schedule(
    group_id: str,
    request: Request,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> Response:
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        db.execute("DELETE FROM recording_policy_overrides WHERE scope_type='group' AND scope_id=?", (group_id,))
        camera_ids = [row["id"] for row in db.execute("SELECT id FROM cameras WHERE group_id=?", (group_id,)).fetchall()]
    await asyncio.gather(*(request.app.state.recorders.sync_camera(camera_id, force_restart=True) for camera_id in camera_ids))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/cameras/{camera_id}/schedule")
def get_camera_schedule(
    camera_id: str,
    _: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, object]:
    with connect() as db:
        camera = db.execute("SELECT id,group_id FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if camera is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        local = db.execute("SELECT timezone_name,schedule_json FROM recording_policy_overrides WHERE scope_type='camera' AND scope_id=?", (camera_id,)).fetchone()
        inherited = None
        if local is None and camera["group_id"]:
            inherited = db.execute("SELECT timezone_name,schedule_json FROM recording_policy_overrides WHERE scope_type='group' AND scope_id=?", (camera["group_id"],)).fetchone()
        source = "camera" if local else "group" if inherited else "camera"
        row = local or inherited
        policy = effective_policy(db, camera_id, datetime.now(timezone.utc))
    return {
        "camera_id": camera_id,
        "inherits_group": local is None and bool(camera["group_id"]),
        "source": source,
        "configured": row is not None,
        "timezone": row["timezone_name"] if row else "UTC",
        "intervals": json.loads(row["schedule_json"]) if row else [],
        "effective_recording_mode": policy["mode"],
    }


@app.put("/api/cameras/{camera_id}/schedule")
async def update_camera_schedule(
    camera_id: str,
    payload: CameraScheduleRequest,
    request: Request,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    if payload.inherit_group:
        intervals = []
    else:
        intervals = validate_schedule(payload.timezone, [item.model_dump() for item in payload.intervals])
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if payload.inherit_group:
            db.execute("DELETE FROM recording_policy_overrides WHERE scope_type='camera' AND scope_id=?", (camera_id,))
        else:
            db.execute(
                "INSERT INTO recording_policy_overrides(scope_type,scope_id,timezone_name,schedule_json) VALUES('camera',?,?,?) "
                "ON CONFLICT(scope_type,scope_id) DO UPDATE SET timezone_name=excluded.timezone_name,schedule_json=excluded.schedule_json,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (camera_id, payload.timezone, json.dumps(intervals, separators=(",", ":"))),
            )
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    return get_camera_schedule(camera_id, _=None)


@app.delete("/api/cameras/{camera_id}/schedule", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera_schedule(
    camera_id: str,
    request: Request,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> Response:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        db.execute("DELETE FROM recording_policy_overrides WHERE scope_type='camera' AND scope_id=?", (camera_id,))
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _camera_import_records(csv_text: str) -> list[dict[str, str | None]]:
    try:
        reader = csv.reader(io.StringIO(csv_text.lstrip("\ufeff"), newline=""), strict=True)
        header = next(reader, None)
        if not header:
            raise HTTPException(status_code=422, detail="CSV must include a header row")
        header = [field.strip().casefold() for field in header]
        allowed = {"name", "stream_url", "recording_mode", "group_name", "onvif_uuid"}
        if len(set(header)) != len(header) or not {"name", "stream_url"}.issubset(header) or not set(header).issubset(allowed):
            raise HTTPException(status_code=422, detail="CSV headers must include name and stream_url and use only recording_mode, group_name, and onvif_uuid as optional columns")
        records: list[dict[str, str | None]] = []
        for row in reader:
            if not row or all(not value.strip() for value in row):
                continue
            values = dict(zip(header, row))
            if len(row) > len(header):
                values["__row_error__"] = "Row has a different number of columns than the header"
            records.append(values)
            if len(records) > 500:
                raise HTTPException(status_code=413, detail="A camera import may contain at most 500 rows")
        if not records:
            raise HTTPException(status_code=422, detail="CSV contains no camera rows")
        return records
    except csv.Error as exc:
        raise HTTPException(status_code=422, detail="CSV data is malformed") from exc


def _camera_import_preview(csv_text: str) -> tuple[list[dict[str, object]], list[CameraImportRow | None]]:
    records = _camera_import_records(csv_text)
    with connect() as db:
        existing = db.execute("SELECT name,onvif_uuid FROM cameras").fetchall()
    existing_names = {row["name"].casefold() for row in existing}
    existing_uuids = {row["onvif_uuid"] for row in existing if row["onvif_uuid"]}
    seen_names: set[str] = set()
    seen_uuids: set[str] = set()
    results: list[dict[str, object]] = []
    validated: list[CameraImportRow | None] = []
    for index, record in enumerate(records, start=2):
        row_errors: list[str] = []
        if record.get("__row_error__"):
            row_errors.append(str(record["__row_error__"]))
        try:
            item = CameraImportRow.model_validate(record)
            if not item.name:
                row_errors.append("Camera name is required")
            try:
                safe_stream_url(item.stream_url)
            except ValueError as exc:
                row_errors.append(str(exc))
            name_key = item.name.casefold()
            if name_key in existing_names or name_key in seen_names:
                row_errors.append("Camera name already exists or is duplicated in this import")
            if item.onvif_uuid:
                if item.onvif_uuid in existing_uuids or item.onvif_uuid in seen_uuids:
                    row_errors.append("ONVIF device identity already exists or is duplicated in this import")
            if not row_errors:
                seen_names.add(name_key)
                if item.onvif_uuid:
                    seen_uuids.add(item.onvif_uuid)
        except ValidationError as exc:
            item = None
            row_errors.extend(error["msg"] for error in exc.errors(include_input=False))
        validated.append(item if not row_errors else None)
        results.append({
            "row": index,
            "name": item.name if item else (str(record.get("name") or "")[:120]),
            "group_name": item.group_name if item else None,
            "status": "accepted" if not row_errors else ("duplicate" if any("already exists or is duplicated" in message for message in row_errors) else "invalid"),
            "errors": row_errors,
        })
    return results, validated


@app.post("/api/cameras/import/preview")
def preview_camera_import(
    payload: CameraImportRequest,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    rows, _ = _camera_import_preview(payload.csv_text)
    return {"rows": rows, "accepted_count": sum(row["status"] == "accepted" for row in rows), "rejected_count": sum(row["status"] != "accepted" for row in rows)}


@app.post("/api/cameras/import/apply")
async def apply_camera_import(
    payload: CameraImportRequest,
    request: Request,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    preview_rows, validated = _camera_import_preview(payload.csv_text)
    probe_limit = asyncio.Semaphore(8)

    async def check(item: CameraImportRow | None) -> dict[str, object] | None:
        if item is None:
            return None
        async with probe_limit:
            return await probe_stream(item.stream_url)

    checks = await asyncio.gather(*(check(item) for item in validated))
    results: list[dict[str, object]] = []
    pending: list[tuple[int, CameraImportRow, dict[str, object]]] = []
    for index, (preview, item, capability) in enumerate(zip(preview_rows, validated, checks)):
        result = dict(preview)
        if item is None:
            results.append(result)
        elif not capability or not capability.get("reachable"):
            result["status"] = "failed"
            result["errors"] = [str((capability or {}).get("reason", "Camera stream could not be opened"))]
            results.append(result)
        else:
            pending.append((index, item, capability))
            results.append(result)

    inserted: list[tuple[int, str, str]] = []
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for index, item, capability in pending:
                group_id = None
                if item.group_name:
                    found = db.execute("SELECT id FROM camera_groups WHERE name=? COLLATE NOCASE", (item.group_name,)).fetchone()
                    if found:
                        group_id = found["id"]
                    else:
                        group_id = str(uuid.uuid4())
                        db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)", (group_id, item.group_name))
                mode = item.recording_mode
                try:
                    camera_id = _insert_camera(
                        db, name=item.name, stream_url=item.stream_url, recording_mode=mode,
                        capability=capability, onvif_uuid=item.onvif_uuid, group_id=group_id,
                    )
                    _record_discovery_probe(db, item.onvif_uuid, capability)
                    if item.onvif_uuid:
                        db.execute("UPDATE discovery_candidates SET linked_camera_id=?,reviewed_at=? WHERE device_uuid=?",(camera_id,datetime.now(timezone.utc).isoformat(),item.onvif_uuid))
                    inserted.append((index,camera_id,mode))
                except sqlite3.IntegrityError:
                    results[index]["status"] = "duplicate"
                    results[index]["errors"] = ["Camera name or ONVIF identity now exists"]
            db.commit()
        except Exception:
            db.rollback()
            raise
    for index,camera_id,mode in inserted:
        await request.app.state.recorders.sync_camera(camera_id)
        results[index]["status"] = "created"
    return {"rows": results, "created_count": len(inserted), "failed_count": sum(row["status"] not in {"created","duplicate"} for row in results)}


def _candidate_view(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "device_uuid": row["device_uuid"],
        "service_urls": json.loads(row["service_urls"]),
        "scopes": json.loads(row["scopes"]),
        "capabilities": json.loads(row["capabilities"]),
        "linked_camera_id": row["linked_camera_id"],
        "reviewed_at": row["reviewed_at"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
    }


@app.post("/api/discovery/scan")
async def scan_cameras(
    payload: DiscoveryScanRequest,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    started = time.monotonic()
    interface_configured = bool(os.environ.get("NVR_DISCOVERY_INTERFACE", "").strip())
    scan_mode = "ip_range" if payload.ip_range else "multicast"
    try:
        if payload.ip_range:
            result = await discover_onvif_ip_range(payload.ip_range)
        else:
            result = await discover_onvif(
                payload.timeout_seconds,
                os.environ.get("NVR_DISCOVERY_INTERFACE", "").strip() or None,
            )
    except ValueError as exc:
        diagnostics.record_event(
            "discovery_scan", outcome="failed", elapsed_ms=min(60000, int((time.monotonic() - started) * 1000)),
            interface_configured=interface_configured,
            error_category="configuration",
            scan_mode=scan_mode,
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        diagnostics.record_event(
            "discovery_scan", outcome="failed", elapsed_ms=min(60000, int((time.monotonic() - started) * 1000)),
            interface_configured=interface_configured, error_category="network", scan_mode=scan_mode,
        )
        raise HTTPException(status_code=503, detail="Camera discovery failed; check the NVR discovery network configuration") from exc

    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        for device in result.devices:
            linked = db.execute("SELECT id FROM cameras WHERE onvif_uuid=?", (device.device_uuid,)).fetchone()
            existing = db.execute("SELECT id,capabilities FROM discovery_candidates WHERE device_uuid=?", (device.device_uuid,)).fetchone()
            candidate_id = existing["id"] if existing else str(uuid.uuid4())
            capabilities = {
                "onvif_discovery": "supported",
                "video_stream": "untested",
                "motion_events": "untested",
                "audio": "untested",
                "ptz": "untested",
            }
            if existing:
                capabilities.update(json.loads(existing["capabilities"]))
            db.execute(
                "INSERT INTO discovery_candidates(id,device_uuid,service_urls,scopes,capabilities,linked_camera_id,first_seen_at,last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(device_uuid) DO UPDATE SET "
                "service_urls=excluded.service_urls,scopes=excluded.scopes,capabilities=excluded.capabilities,"
                "linked_camera_id=COALESCE(excluded.linked_camera_id,discovery_candidates.linked_camera_id),last_seen_at=excluded.last_seen_at",
                (candidate_id, device.device_uuid, json.dumps(device.service_urls), json.dumps(device.scopes),
                 json.dumps(capabilities), linked["id"] if linked else None, now, now),
            )
        rows = db.execute("SELECT * FROM discovery_candidates ORDER BY last_seen_at DESC,device_uuid").fetchall()
    partial_failures = [f"Ignored {result.ignored_responses} malformed or unrecognized response(s)"] if result.ignored_responses else []
    diagnostics.record_event(
        "discovery_scan", outcome="completed", elapsed_ms=min(60000, int((time.monotonic() - started) * 1000)),
        interface_configured=interface_configured, discovered_count=len(result.devices), ignored_responses=result.ignored_responses,
        scan_mode=scan_mode, targets_scanned=result.targets_scanned,
    )
    return {
        "scan_mode": scan_mode,
        "targets_scanned": result.targets_scanned,
        "discovered_count": len(result.devices),
        "ignored_responses": result.ignored_responses,
        "partial_failures": partial_failures,
        "candidates": [_candidate_view(row) for row in rows],
    }


@app.get("/api/discovery/candidates")
def list_discovery_candidates(_: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    with connect() as db:
        rows = db.execute("SELECT * FROM discovery_candidates ORDER BY last_seen_at DESC,device_uuid").fetchall()
    return {"candidates": [_candidate_view(row) for row in rows]}


@app.post("/api/discovery/candidates/{candidate_id}/link")
def link_discovered_camera(
    candidate_id: str,
    payload: DiscoveredCameraLinkRequest,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    with connect() as db:
        candidate = db.execute("SELECT device_uuid,linked_camera_id FROM discovery_candidates WHERE id=?", (candidate_id,)).fetchone()
        if candidate is None:
            raise HTTPException(status_code=404, detail="Discovery candidate not found")
        if candidate["linked_camera_id"] and candidate["linked_camera_id"] != payload.camera_id:
            raise HTTPException(status_code=409, detail="This discovered device is already linked to another camera")
        cursor = db.execute("SELECT id FROM cameras WHERE id=?", (payload.camera_id,)).fetchone()
        if cursor is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if not candidate["device_uuid"].startswith("ipscan:"):
            try:
                db.execute("UPDATE cameras SET onvif_uuid=?,updated_at=? WHERE id=?", (candidate["device_uuid"], datetime.now(timezone.utc).isoformat(), payload.camera_id))
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="This ONVIF identity is already assigned to another camera") from exc
        db.execute("UPDATE discovery_candidates SET linked_camera_id=?,reviewed_at=? WHERE id=?", (payload.camera_id, datetime.now(timezone.utc).isoformat(), candidate_id))
        row = db.execute("SELECT id,name,recording_mode FROM cameras WHERE id=?", (payload.camera_id,)).fetchone()
    return {"camera": dict(row), "device_uuid": candidate["device_uuid"]}


@app.post("/api/discovery/candidates/{candidate_id}/add", status_code=status.HTTP_201_CREATED)
async def add_discovered_camera(
    candidate_id: str,
    payload: DiscoveredCameraAddRequest,
    request: Request,
    user: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    with connect() as db:
        candidate = db.execute("SELECT * FROM discovery_candidates WHERE id=?", (candidate_id,)).fetchone()
    if candidate is None:
        raise HTTPException(status_code=404, detail="Discovery candidate not found")
    if candidate["linked_camera_id"]:
        raise HTTPException(status_code=409, detail="This discovered device is already linked to a camera")
    try:
        camera = await create_camera(
            CameraCreateRequest(
                name=payload.name,
                stream_url=payload.stream_url,
                camera_ip=payload.camera_ip,
                camera_username=payload.camera_username,
                camera_password=payload.camera_password,
                camera_brand=payload.camera_brand,
                onvif_profile_token=payload.onvif_profile_token,
                recording_mode=payload.recording_mode,
                recording_enabled=payload.recording_enabled,
                motion_service_url=payload.device_service_url or (json.loads(candidate["service_urls"])[0] if candidate["service_urls"] else None),
                motion_username=payload.motion_username or payload.camera_username,
                motion_password=payload.motion_password or payload.camera_password,
                motion_pre_roll_seconds=payload.motion_pre_roll_seconds,
                motion_post_roll_seconds=payload.motion_post_roll_seconds,
                motion_fallback_enabled=payload.motion_fallback_enabled,
                motion_zones=payload.motion_zones,
                motion_sensitivity=payload.motion_sensitivity,
                onvif_uuid=None if candidate["device_uuid"].startswith("ipscan:") else candidate["device_uuid"],
            ),
            request,
            user,
        )
    except HTTPException as exc:
        if exc.status_code == 422:
            with connect() as db:
                _record_discovery_failure(db, candidate["device_uuid"])
        raise
    with connect() as db:
        reviewed_at = datetime.now(timezone.utc).isoformat()
        db.execute("UPDATE discovery_candidates SET linked_camera_id=?,reviewed_at=? WHERE id=?", (camera["id"], reviewed_at, candidate_id))
    return camera


@app.post("/api/cameras", status_code=status.HTTP_201_CREATED)
async def create_camera(
    payload: CameraCreateRequest,
    request: Request,
    _: Annotated[sqlite3.Row, Depends(administrator)],
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
            camera_id = _insert_camera(
                db, name=name, stream_url=stream_url, recording_mode=payload.recording_mode,
                capability=capability, onvif_uuid=payload.onvif_uuid,
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
            _record_discovery_probe(db, payload.onvif_uuid, capability)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="A camera with that name or ONVIF identity already exists") from exc
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    with connect() as db:
        row = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    return _camera_view(row)


@app.patch("/api/cameras/{camera_id}")
async def update_camera(
    camera_id: str,
    payload: CameraUpdateRequest,
    request: Request,
    user: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    if not any(name in payload.model_fields_set for name in ("recording_mode", "recording_enabled", "motion_pre_roll_seconds", "motion_post_roll_seconds", "audio_enabled")):
        raise HTTPException(status_code=422, detail="Specify a recording mode, enablement, buffer duration, or audio setting")
    with connect() as db:
        row = db.execute("SELECT recording_mode,recording_enabled,motion_service_url,motion_fallback_enabled,audio_supported,audio_enabled FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if payload.audio_enabled and row["audio_supported"] is not True and row["audio_supported"] != 1:
            message = "Probe the camera audio capability before enabling audio" if row["audio_supported"] is None else "This camera stream has no supported audio track"
            raise HTTPException(status_code=422, detail=message)
        mode = payload.recording_mode or row["recording_mode"]
        enabled = payload.recording_enabled if payload.recording_enabled is not None else bool(row["recording_enabled"])
        if mode == "disabled":
            enabled = False
        assignments = ["recording_mode=?", "recording_enabled=?", "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"]
        values: list[object] = [mode, int(enabled)]
        audio_enabled = payload.audio_enabled if payload.audio_enabled is not None else bool(row["audio_enabled"])
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
        db.execute(f"UPDATE cameras SET {','.join(assignments)} WHERE id=?", values)
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    with connect() as db:
        row = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    return _camera_view(row)


@app.post("/api/cameras/bulk-update")
async def bulk_update_cameras(
    payload: CameraBulkUpdateRequest,
    request: Request,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    if "group_name" not in payload.model_fields_set and payload.recording_mode is None and payload.recording_enabled is None:
        raise HTTPException(status_code=422, detail="Specify a group assignment, recording mode, or enablement")
    if len(set(payload.camera_ids)) != len(payload.camera_ids):
        raise HTTPException(status_code=422, detail="Camera IDs must be unique")
    with connect() as db:
        rows = db.execute(f"SELECT id FROM cameras WHERE id IN ({','.join('?' for _ in payload.camera_ids)})", payload.camera_ids).fetchall()
        if len(rows) != len(payload.camera_ids):
            raise HTTPException(status_code=404, detail="One or more cameras were not found")
        group_id = None
        if "group_name" in payload.model_fields_set and payload.group_name:
            group = db.execute("SELECT id FROM camera_groups WHERE name=? COLLATE NOCASE", (payload.group_name,)).fetchone()
            if group:
                group_id = group["id"]
            else:
                group_id = str(uuid.uuid4())
                db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)", (group_id, payload.group_name))

        assignments = []
        values: list[object] = []
        if "group_name" in payload.model_fields_set:
            assignments.append("group_id=?")
            values.append(group_id)
        if payload.recording_mode is not None:
            assignments.append("recording_mode=?")
            values.append(payload.recording_mode)
            if payload.recording_mode == "disabled":
                assignments.extend(("recording_enabled=0", "recording_status='stopped'", "last_error=NULL"))
        if payload.recording_enabled is not None:
            assignments.append("recording_enabled=?")
            values.append(int(payload.recording_enabled and payload.recording_mode != "disabled"))
            assignments.extend(("recording_status=CASE WHEN ?=1 THEN 'connecting' ELSE 'stopped' END", "last_error=CASE WHEN ?=1 AND recording_mode='motion' AND motion_service_url IS NULL AND motion_fallback_enabled=0 THEN 'Configure an ONVIF event service or enable server motion fallback' ELSE NULL END"))
            effective_enabled = int(payload.recording_enabled and payload.recording_mode != "disabled")
            values.extend((effective_enabled, effective_enabled))
        elif payload.recording_mode is not None and payload.recording_mode != "disabled":
            assignments.extend(("recording_status=CASE WHEN recording_enabled=1 THEN 'connecting' ELSE 'stopped' END", "last_error=NULL"))
        assignments.append("updated_at=?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.extend(payload.camera_ids)
        db.execute(f"UPDATE cameras SET {','.join(assignments)} WHERE id IN ({','.join('?' for _ in payload.camera_ids)})", values)
    if "group_name" in payload.model_fields_set or payload.recording_mode is not None or payload.recording_enabled is not None:
        await asyncio.gather(*(request.app.state.recorders.sync_camera(camera_id, force_restart=True) for camera_id in payload.camera_ids))
    with connect() as db:
        updated = db.execute(
            f"SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id IN ({','.join('?' for _ in payload.camera_ids)}) ORDER BY c.name COLLATE NOCASE",
            payload.camera_ids,
        ).fetchall()
    return {"cameras": [_camera_view(row) for row in updated]}


@app.post("/api/cameras/{camera_id}/credentials")
async def rotate_camera_credentials(
    camera_id: str,
    payload: CameraCredentialRotateRequest,
    request: Request,
    _: Annotated[sqlite3.Row, Depends(administrator)],
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
    return _camera_view(row)


@app.post("/api/cameras/{camera_id}/audio/probe")
async def probe_camera_audio(
    camera_id: str,
    request: Request,
    user: Annotated[sqlite3.Row, Depends(administrator)],
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


@app.post("/api/cameras/{camera_id}/motion-source")
async def configure_motion_source(
    camera_id: str,
    payload: MotionSourceUpdateRequest,
    request: Request,
    _: Annotated[sqlite3.Row, Depends(administrator)],
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
    return _camera_view(camera)


@app.get("/api/cameras/{camera_id}/motion-events")
def list_camera_motion_events(
    camera_id: str,
    _: Annotated[sqlite3.Row, Depends(current_user)],
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


def _ensure_ai_settings(camera_id: str) -> sqlite3.Row:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        db.execute("INSERT OR IGNORE INTO ai_camera_settings(camera_id) VALUES(?)", (camera_id,))
        row = db.execute("SELECT * FROM ai_camera_settings WHERE camera_id=?", (camera_id,)).fetchone()
    return row


def _ai_runtime_model_view() -> dict[str, object]:
    with connect() as db:
        runtime = db.execute("SELECT provider,model_id,model_version,validated_at FROM ai_runtime_settings WHERE singleton_id=1").fetchone()
    provider = str(runtime["provider"]) if runtime else "CPU"
    model_id = str(runtime["model_id"]) if runtime else MODEL_ID
    stored_version = str(runtime["model_version"]) if runtime else MODEL_VERSION
    options = model_options()
    selected = next((item for item in options if item["id"] == model_id), None)
    content_matches = bool(selected and selected["available"] and selected["version"] == stored_version)
    availability = detector_availability(provider, model_id)
    validated = bool(runtime and runtime["validated_at"] and content_matches)
    return {
        "provider": provider,
        "model_id": model_id,
        "model_version": stored_version,
        "runtime": f"{MODEL_RUNTIME} {provider}",
        "available": bool(availability["available"] and content_matches),
        "reason": availability["reason"] if content_matches else "Model files changed since validation; validate the selected model before activation",
        "validated": validated,
        "validated_at": runtime["validated_at"] if runtime else None,
        "provider_options": openvino_provider_options(),
        "model_options": options,
    }


@app.get("/api/cameras/{camera_id}/ai")
def get_ai_detection_config(camera_id: str, _: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    row = _ensure_ai_settings(camera_id)
    effective = effective_camera_config(camera_id) or {}
    return {
        "camera_id": camera_id,
        "enabled": bool(effective.get("enabled", False)),
        "analysis_fps": effective.get("analysis_fps", 0.5),
        "confidence_threshold": effective.get("confidence_threshold", 0.55),
        "use_substream": bool(effective.get("use_substream", True)),
        "zones": json.loads(effective.get("zones_json", "[]")),
        "config_source": effective.get("config_source", "default"),
        "config_override": bool(row["config_override"]),
        "status": row["status"],
        "last_attempt_at": row["last_attempt_at"],
        "last_success_at": row["last_success_at"],
        "last_error": row["last_error"],
        "model": _ai_runtime_model_view(),
        "coverage_note": "No detections do not prove an object was absent; inspect AI coverage status and model results.",
    }


@app.put("/api/cameras/{camera_id}/ai")
async def update_ai_detection_config(
    camera_id: str,
    payload: AIDetectionConfigRequest,
    request: Request,
    user: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    existing = _ensure_ai_settings(camera_id)
    zones = [zone.model_dump() for zone in payload.zones]
    active_model = _ai_runtime_model_view()
    with connect() as db:
        camera = db.execute("SELECT group_id,live_substream_url_ciphertext FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if payload.inherit_group and not camera["group_id"]:
            raise HTTPException(status_code=422, detail="Assign this camera to a group before inheriting its AI settings")
        if not payload.inherit_group and payload.enabled and payload.use_substream and not camera["live_substream_url_ciphertext"]:
            raise HTTPException(status_code=422, detail="Configure and validate a lower-resolution camera profile, or select the main stream")
        db.execute(
            "UPDATE ai_camera_settings SET config_override=?,enabled=?,analysis_fps=?,confidence_threshold=?,use_substream=?,zones_json=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE camera_id=?",
            (int(not payload.inherit_group), int(payload.enabled), payload.analysis_fps, payload.confidence_threshold, int(payload.use_substream), json.dumps(zones, separators=(",", ":")), camera_id),
        )
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "ai_detection_configured", "camera", camera_id,
                    json.dumps({"enabled": payload.enabled, "inherit_group": payload.inherit_group, "analysis_fps": payload.analysis_fps, "model_id": active_model["model_id"], "model_version": active_model["model_version"], "provider": active_model["provider"]}, separators=(",", ":"))))
    await request.app.state.detectors.sync_camera(camera_id, force_restart=True)
    return get_ai_detection_config(camera_id, _=user)


def _group_ai_view(group_id: str) -> dict[str, object]:
    with connect() as db:
        group = db.execute("SELECT id,name FROM camera_groups WHERE id=?", (group_id,)).fetchone()
        if group is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        config = db.execute("SELECT * FROM ai_group_settings WHERE group_id=?", (group_id,)).fetchone()
        camera_count = db.execute("SELECT COUNT(*) FROM cameras WHERE group_id=?", (group_id,)).fetchone()[0]
        inherited_count = db.execute("SELECT COUNT(*) FROM cameras c LEFT JOIN ai_camera_settings s ON s.camera_id=c.id WHERE c.group_id=? AND COALESCE(s.config_override,0)=0", (group_id,)).fetchone()[0]
    return {
        "group_id": group_id, "group_name": group["name"], "configured": config is not None,
        "enabled": bool(config["enabled"]) if config else False,
        "analysis_fps": config["analysis_fps"] if config else 0.5,
        "confidence_threshold": config["confidence_threshold"] if config else 0.55,
        "use_substream": bool(config["use_substream"]) if config else True,
        "zones": json.loads(config["zones_json"]) if config else [],
        "camera_count": camera_count, "inherited_camera_count": inherited_count,
        "model": _ai_runtime_model_view(),
    }


@app.get("/api/camera-groups/{group_id}/ai")
def get_group_ai_config(group_id: str, _: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    return _group_ai_view(group_id)


@app.put("/api/camera-groups/{group_id}/ai")
async def update_group_ai_config(
    group_id: str,
    payload: AIDetectionConfigRequest,
    request: Request,
    user: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    zones = [zone.model_dump() for zone in payload.zones]
    active_model = _ai_runtime_model_view()
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        if payload.enabled and payload.use_substream:
            missing = db.execute("SELECT COUNT(*) FROM cameras c LEFT JOIN ai_camera_settings s ON s.camera_id=c.id WHERE c.group_id=? AND c.enabled=1 AND COALESCE(s.config_override,0)=0 AND c.live_substream_url_ciphertext IS NULL", (group_id,)).fetchone()[0]
            if missing:
                raise HTTPException(status_code=422, detail=f"{missing} inherited camera(s) do not have a validated lower-resolution profile; choose the main stream or configure the profiles")
        db.execute(
            "INSERT INTO ai_group_settings(group_id,enabled,analysis_fps,confidence_threshold,use_substream,zones_json,updated_at) VALUES(?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ON CONFLICT(group_id) DO UPDATE SET enabled=excluded.enabled,analysis_fps=excluded.analysis_fps,confidence_threshold=excluded.confidence_threshold,use_substream=excluded.use_substream,zones_json=excluded.zones_json,updated_at=excluded.updated_at",
            (group_id, int(payload.enabled), payload.analysis_fps, payload.confidence_threshold, int(payload.use_substream), json.dumps(zones, separators=(",", ":"))),
        )
        camera_ids = [row[0] for row in db.execute("SELECT c.id FROM cameras c LEFT JOIN ai_camera_settings s ON s.camera_id=c.id WHERE c.group_id=? AND c.enabled=1 AND COALESCE(s.config_override,0)=0", (group_id,)).fetchall()]
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "ai_group_configured", "camera_group", group_id,
                    json.dumps({"enabled": payload.enabled, "inherited_camera_count": len(camera_ids), "analysis_fps": payload.analysis_fps, "model_id": active_model["model_id"], "model_version": active_model["model_version"], "provider": active_model["provider"]}, separators=(",", ":"))))
    for camera_id in camera_ids:
        await request.app.state.detectors.sync_camera(camera_id, force_restart=True)
    return _group_ai_view(group_id)


@app.get("/api/ai/settings")
def get_ai_runtime_settings(_: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    with connect() as db:
        row = db.execute("SELECT max_active_cameras,updated_at FROM ai_runtime_settings WHERE singleton_id=1").fetchone()
    return {"max_active_cameras": int(row["max_active_cameras"]), "inflight_inferences": 1, "updated_at": row["updated_at"],
            "resource_note": "Inference calls run through one bounded slot to preserve headroom for recording. Cameras beyond the active limit are marked skipped; no unbounded analysis queue is maintained.",
            "model": _ai_runtime_model_view()}


@app.post("/api/ai/settings/validate")
async def validate_ai_runtime_settings(payload: AIRuntimeValidationRequest, _: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    result = await asyncio.to_thread(validate_detector, payload.provider, payload.model_id)
    return {**result, "activation_changed": False, "gpu_certification_note": "GPU is certified only for this exact host, OpenVINO runtime, model, and active recording workload after validation."}


@app.put("/api/ai/settings")
async def update_ai_runtime_settings(payload: AIRuntimeSettingsRequest, request: Request, user: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    with connect() as db:
        current = db.execute("SELECT provider,model_id,model_version FROM ai_runtime_settings WHERE singleton_id=1").fetchone()
    provider = payload.provider or str(current["provider"])
    model_id = payload.model_id or str(current["model_id"])
    model_version = str(current["model_version"])
    validated_at: str | None = None
    if payload.provider is not None or payload.model_id is not None:
        validation = await asyncio.to_thread(validate_detector, provider, model_id)
        if not validation["valid"]:
            raise HTTPException(status_code=422, detail=f"AI runtime was not changed: {validation['reason']}")
        model_version = str(validation["model_version"])
        validated_at = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("UPDATE ai_runtime_settings SET max_active_cameras=?,provider=?,model_id=?,model_version=?,validated_at=COALESCE(?,validated_at),updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE singleton_id=1",
                   (payload.max_active_cameras, provider, model_id, model_version, validated_at))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "ai_runtime_configured", "ai_runtime", "local",
                    json.dumps({"max_active_cameras": payload.max_active_cameras, "provider": provider, "model_id": model_id, "model_version": model_version, "validated": validated_at is not None}, separators=(",", ":"))))
    await request.app.state.detectors.reconcile()
    return get_ai_runtime_settings(_=user)


@app.get("/api/ai/events")
def search_ai_observations(
    _: Annotated[sqlite3.Row, Depends(current_user)],
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
def get_ai_detection_coverage(camera_id: str, _: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    _ensure_ai_settings(camera_id)
    with connect() as db:
        rows = db.execute("SELECT state,starts_at,ends_at,detail FROM ai_coverage WHERE camera_id=? ORDER BY starts_at DESC LIMIT 500", (camera_id,)).fetchall()
        setting = db.execute("SELECT status,last_attempt_at,last_success_at,last_error FROM ai_camera_settings WHERE camera_id=?", (camera_id,)).fetchone()
    return {"status": setting["status"], "last_attempt_at": setting["last_attempt_at"], "last_success_at": setting["last_success_at"], "last_error": setting["last_error"], "intervals": [dict(row) for row in rows], "coverage_note": "A failed or delayed interval is unknown coverage; detections only describe sampled frames."}


@app.delete("/api/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(
    camera_id: str,
    request: Request,
    user: Annotated[sqlite3.Row, Depends(administrator)],
) -> Response:
    with connect() as db:
        exists = db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone()
        protected=db.execute("SELECT 1 FROM recording_holds WHERE camera_id=? AND released_at IS NULL AND (expires_at IS NULL OR expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) LIMIT 1",(camera_id,)).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    if protected:
        raise HTTPException(status_code=409,detail="Camera has preserved footage; release its holds before deleting the camera")
    await request.app.state.recorders.remove(camera_id)
    root = recordings_path()
    try:
        from .storage import camera_recordings_dir, recording_file_path
        with connect() as db:
            media_paths=[row["relative_path"] for row in db.execute("SELECT relative_path FROM recording_segments WHERE camera_id=?",(camera_id,)).fetchall()]
        for relative_path in media_paths:
            media_path=recording_file_path(relative_path)
            from .retention import unlink_if_unleased
            if not unlink_if_unleased(media_path):
                raise OSError("Recorded footage is currently being played")
        camera_dir = camera_recordings_dir(camera_id).resolve()
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Assigned recording volume is unavailable; camera configuration was retained") from exc
    if root in camera_dir.parents and camera_dir.is_dir():
        try:
            shutil.rmtree(camera_dir)
        except OSError as exc:
            raise HTTPException(status_code=500, detail="Stored footage could not be removed; camera configuration was retained") from exc
    with connect() as db:
        db.execute("DELETE FROM recording_policy_overrides WHERE scope_type='camera' AND scope_id=?", (camera_id,))
        db.execute("DELETE FROM retention_policy_overrides WHERE scope_type='camera' AND scope_id=?", (camera_id,))
        db.execute("DELETE FROM cameras WHERE id=?", (camera_id,))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",(int(user["id"]),"delete","camera",camera_id,"{}"))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/cameras/{camera_id}/segments")
def list_camera_segments(
    camera_id: str,
    _: Annotated[sqlite3.Row, Depends(current_user)],
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
    _: Annotated[sqlite3.Row, Depends(current_user)],
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
    _: Annotated[sqlite3.Row, Depends(current_user)],
    state: str | None = Query(default=None, pattern="^(active|acknowledged|recovered)$"),
) -> dict[str, object]:
    with connect() as db:
        return {"incidents": list_incidents(db, state=state)}


@app.post("/api/incidents/{incident_id}/acknowledge")
def post_incident_acknowledgement(
    incident_id: str,
    user: Annotated[sqlite3.Row, Depends(current_user)],
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
def get_webhook_event_types(_: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    return {"event_types": sorted(WEBHOOK_EVENT_TYPES)}


@app.get("/api/heartbeat")
def get_heartbeat_status(_: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    return heartbeat_status()


@app.put("/api/heartbeat")
def put_heartbeat_config(payload: HeartbeatConfigRequest, user: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    try:
        result, secret = configure_heartbeat(payload.target_url, payload.interval_seconds, payload.allow_private_receiver, int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "heartbeat_configured", "heartbeat", "1", json.dumps({"interval_seconds": payload.interval_seconds, "allow_private_receiver": payload.allow_private_receiver})))
    return {"heartbeat": result, "signing_secret": secret, "secret_notice": "A new signing secret is shown once. Store it at the external receiver." if secret else None}


@app.post("/api/heartbeat/test", status_code=status.HTTP_202_ACCEPTED)
def post_heartbeat_test(user: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, str]:
    if not queue_heartbeat_test():
        raise HTTPException(status_code=409, detail="Configure and enable an external heartbeat receiver first")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "heartbeat_test_requested", "heartbeat", "1", "{}"))
    return {"status": "queued"}


@app.post("/api/heartbeat/rotate-secret")
def post_heartbeat_secret_rotation(user: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, str]:
    secret = rotate_heartbeat_secret()
    if secret is None:
        raise HTTPException(status_code=409, detail="Configure an enabled heartbeat receiver first")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "heartbeat_secret_rotated", "heartbeat", "1", "{}"))
    return {"signing_secret": secret, "secret_notice": "This secret is shown once. Update the receiver before the next heartbeat."}


@app.delete("/api/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
def delete_heartbeat_config(user: Annotated[sqlite3.Row, Depends(administrator)]) -> Response:
    disable_heartbeat()
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "heartbeat_disabled", "heartbeat", "1", "{}"))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/webhooks")
def get_webhooks(_: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    return {"webhooks": list_webhook_endpoints()}


@app.post("/api/webhooks", status_code=status.HTTP_201_CREATED)
def post_webhook(payload: WebhookCreateRequest, user: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    try:
        endpoint, secret = create_webhook_endpoint(payload.name, payload.url, payload.events, payload.allow_private_receiver, int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_created", "webhook", endpoint["id"], json.dumps({"name": endpoint["name"], "events": endpoint["events"], "allow_private_receiver": endpoint["allow_private_receiver"]})))
    return {"webhook": endpoint, "signing_secret": secret, "secret_notice": "This secret is shown once. Store it at the receiver to verify signed deliveries."}


@app.patch("/api/webhooks/{webhook_id}")
def patch_webhook(webhook_id: str, payload: WebhookUpdateRequest, user: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
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
def post_webhook_secret_rotation(webhook_id: str, user: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, str]:
    secret = rotate_webhook_secret(webhook_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_secret_rotated", "webhook", webhook_id, "{}"))
    return {"signing_secret": secret, "secret_notice": "This secret is shown once. Update the receiver before the next delivery."}


@app.delete("/api/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_webhook(webhook_id: str, user: Annotated[sqlite3.Row, Depends(administrator)]) -> Response:
    if not delete_webhook_endpoint(webhook_id):
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_deleted", "webhook", webhook_id, "{}"))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/webhooks/{webhook_id}/test", status_code=status.HTTP_202_ACCEPTED)
def post_webhook_test(webhook_id: str, user: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    event_id = enqueue_webhook_test(webhook_id, int(user["id"]))
    if event_id is None:
        raise HTTPException(status_code=404, detail="Enabled webhook endpoint not found")
    with connect() as db:
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "webhook_test_requested", "webhook", webhook_id, json.dumps({"event_id": event_id})))
    return {"event_id": event_id, "status": "queued"}


@app.get("/api/webhooks/deliveries")
def get_webhook_deliveries(
    _: Annotated[sqlite3.Row, Depends(administrator)],
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    return {"deliveries": list_webhook_deliveries(limit)}


@app.post("/api/playback/timeline")
def synchronized_playback_timeline(
    payload: SynchronizedPlaybackRequest,
    _: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, object]:
    start = payload.start.astimezone(timezone.utc)
    end = payload.end.astimezone(timezone.utc)
    start_text, end_text = start.isoformat(), end.isoformat()
    cameras: list[dict[str, object]] = []
    with connect() as db:
        for camera_id in payload.camera_ids:
            camera = db.execute("SELECT id,name,recording_status FROM cameras WHERE id=?", (camera_id,)).fetchone()
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
                            "segments": segments, "coverage": coverage, "motion_events": events, "gaps": gaps})
    return {"start": start_text, "end": end_text, "cameras": cameras}


@app.get("/api/cameras/{camera_id}/segments/{segment_id}/hold")
def segment_hold_status(camera_id: str, segment_id: int, _: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    try:
        return get_segment_hold(camera_id,segment_id)
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc


@app.post("/api/cameras/{camera_id}/segments/{segment_id}/hold",status_code=status.HTTP_201_CREATED)
def preserve_camera_segment(camera_id: str, segment_id: int, payload: HoldRequest, user: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
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
def release_camera_segment(camera_id: str, segment_id: int, user: Annotated[sqlite3.Row, Depends(administrator)]) -> Response:
    try:
        release_segment_hold(camera_id,segment_id,int(user["id"]))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    return Response(status_code=204)


@app.delete("/api/cameras/{camera_id}/segments/{segment_id}",status_code=status.HTTP_204_NO_CONTENT)
def delete_camera_segment(camera_id: str, segment_id: int, user: Annotated[sqlite3.Row, Depends(administrator)]) -> Response:
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
    _: Annotated[sqlite3.Row, Depends(current_user)],
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
    _: Annotated[sqlite3.Row, Depends(current_user)],
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
        return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except asyncio.TimeoutError as exc:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        raise HTTPException(status_code=503, detail="Snapshot extraction timed out") from exc
    finally:
        release_playback(path)


@app.get("/api/exports")
def get_exports(user: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    return {"exports": list_exports(int(user["id"]), user["role"] == "administrator")}


@app.post("/api/exports", status_code=status.HTTP_202_ACCEPTED)
async def create_export(
    payload: ExportRequest,
    request: Request,
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, object]:
    start, end = payload.start.astimezone(timezone.utc), payload.end.astimezone(timezone.utc)
    try:
        job_id = create_export_record(int(user["id"]), payload.camera_ids, start, end)
    except LookupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
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
def get_export_status(job_id: str, user: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    row = get_export(job_id, int(user["id"]), user["role"] == "administrator")
    if row is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    return _job_view(row)


@app.post("/api/exports/{job_id}/cancel")
async def cancel_export(job_id: str, request: Request, user: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    try:
        state = request_cancel(job_id, int(user["id"]), user["role"] == "administrator")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if state == "cancel_requested":
        await request.app.state.exports.cancel(job_id)
    row = get_export(job_id, int(user["id"]), user["role"] == "administrator")
    return _job_view(row)


@app.get("/api/exports/{job_id}/download")
def download_export(job_id: str, user: Annotated[sqlite3.Row, Depends(current_user)]) -> FileResponse:
    row = get_export(job_id, int(user["id"]), user["role"] == "administrator")
    if row is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    try:
        path = export_output_path(row)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=410, detail="Export file is unavailable") from exc
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Export file is unavailable")
    acquire_playback(path)
    return FileResponse(path, media_type="application/zip", filename=f"nvr-export-{job_id}.zip", background=BackgroundTask(release_playback, path))


@app.post("/api/exports/{job_id}/verify")
async def verify_export_evidence(job_id: str, request: Request, user: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
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
