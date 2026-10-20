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
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware

from .security import hash_password, new_session_token, token_digest, verify_password
from .store import connect, database_path, initialize
from .credentials import encrypt, decrypt
from .recording import RecordingManager, masked_stream_url, probe_stream, recordings_path, safe_stream_url
from .discovery import DiscoveryResult, discover_onvif
from . import diagnostics

APP_VERSION = "0.1.0"
COOKIE_NAME = "nvr_session"
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.@-]{3,128}$")


def _secure_cookie() -> bool:
    if os.environ.get("NVR_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}:
        return True
    return os.environ.get("NVR_PUBLIC_URL", "").lower().startswith("https://")


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.umask(0o077)
    initialize()
    app.state.recorders = RecordingManager()
    await app.state.recorders.start()
    yield
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


class CameraProbeRequest(BaseModel):
    stream_url: str = Field(min_length=8, max_length=2048)


class CameraCreateRequest(CameraProbeRequest):
    name: str = Field(min_length=1, max_length=120)
    recording_mode: Literal["motion", "continuous", "disabled"] = "motion"
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

    @field_validator("group_name")
    @classmethod
    def trim_group_name(cls, value: str | None) -> str | None:
        value = value.strip() if isinstance(value, str) else value
        return (value or None) if isinstance(value, str) else value


class CameraCredentialRotateRequest(CameraProbeRequest):
    pass


class CameraUpdateRequest(BaseModel):
    recording_mode: Literal["motion", "continuous", "disabled"]


class CameraGroupCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class DiscoveryScanRequest(BaseModel):
    timeout_seconds: float = Field(default=5, ge=0.5, le=15)


class DiagnosticCaptureRequest(BaseModel):
    enabled: bool
    hours: int = Field(default=4, ge=1, le=24)


class DiscoveredCameraAddRequest(CameraProbeRequest):
    name: str = Field(min_length=1, max_length=120)
    recording_mode: Literal["motion", "continuous", "disabled"] = "motion"


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


def _camera_view(row: sqlite3.Row) -> dict[str, object]:
    try:
        visible_url = masked_stream_url(decrypt(row["stream_url_ciphertext"]))
    except RuntimeError:
        visible_url = "rtsp://credentials-unavailable"
    return {
        "id": row["id"],
        "name": row["name"],
        "group_name": row["group_name"] if "group_name" in row.keys() else None,
        "stream_url": visible_url,
        "enabled": bool(row["enabled"]),
        "recording_mode": row["recording_mode"],
        "recording_status": row["recording_status"],
        "video": {"codec": row["video_codec"], "width": row["video_width"], "height": row["video_height"]},
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
) -> str:
    """Persist a camera only after its stream has passed the shared probe."""
    video = capability["video"]
    if not isinstance(video, dict):
        raise ValueError("A successful stream probe must include video metadata")
    camera_id = str(uuid.uuid4())
    status = "unsupported" if recording_mode == "motion" else ("stopped" if recording_mode == "disabled" else "connecting")
    error = "Motion detection is not enabled in this implementation slice" if recording_mode == "motion" else None
    db.execute(
        "INSERT INTO cameras(id,name,onvif_uuid,group_id,stream_url_ciphertext,recording_mode,recording_status,last_error,video_codec,video_width,video_height,last_probe_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (camera_id, name, onvif_uuid, group_id, encrypt(stream_url.strip()), recording_mode, status, error,
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
        safe_stream_url(payload.stream_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    started = time.monotonic()
    result = await probe_stream(payload.stream_url)
    diagnostics.record_event(
        "stream_probe", outcome="success" if result.get("reachable") else "unavailable",
        elapsed_ms=min(60000, int((time.monotonic() - started) * 1000)),
    )
    return result


@app.get("/api/cameras")
def list_cameras(_: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    with connect() as db:
        rows = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id ORDER BY c.name COLLATE NOCASE").fetchall()
    return {"cameras": [_camera_view(row) for row in rows]}


@app.get("/api/camera-groups")
def list_camera_groups(_: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, object]:
    with connect() as db:
        groups = db.execute("SELECT id,name FROM camera_groups ORDER BY name COLLATE NOCASE").fetchall()
    return {"groups": [dict(group) for group in groups]}


@app.post("/api/camera-groups", status_code=status.HTTP_201_CREATED)
def create_camera_group(
    payload: CameraGroupCreateRequest,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, str]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Group name is required")
    group_id = str(uuid.uuid4())
    with connect() as db:
        try:
            db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)", (group_id, name))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="A camera group with that name already exists") from exc
    return {"id": group_id, "name": name}


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
        if mode == "continuous":
            request.app.state.recorders.ensure(camera_id)
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
    try:
        result: DiscoveryResult = await discover_onvif(
            payload.timeout_seconds,
            os.environ.get("NVR_DISCOVERY_INTERFACE", "").strip() or None,
        )
    except (OSError, ValueError) as exc:
        diagnostics.record_event(
            "discovery_scan", outcome="failed", elapsed_ms=min(60000, int((time.monotonic() - started) * 1000)),
            interface_configured=interface_configured,
            error_category="configuration" if isinstance(exc, ValueError) else "network",
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
    )
    return {
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
        try:
            cursor = db.execute("UPDATE cameras SET onvif_uuid=?,updated_at=? WHERE id=?", (candidate["device_uuid"], datetime.now(timezone.utc).isoformat(), payload.camera_id))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="This ONVIF identity is already assigned to another camera") from exc
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Camera not found")
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
                recording_mode=payload.recording_mode,
                onvif_uuid=candidate["device_uuid"],
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
        safe_stream_url(payload.stream_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    capability = await probe_stream(payload.stream_url)
    if not capability.get("reachable"):
        raise HTTPException(status_code=422, detail=str(capability.get("reason", "Camera stream could not be opened")))
    with connect() as db:
        try:
            camera_id = _insert_camera(
                db, name=name, stream_url=payload.stream_url, recording_mode=payload.recording_mode,
                capability=capability, onvif_uuid=payload.onvif_uuid,
            )
            _record_discovery_probe(db, payload.onvif_uuid, capability)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="A camera with that name or ONVIF identity already exists") from exc
    if payload.recording_mode == "continuous":
        request.app.state.recorders.ensure(camera_id)
    with connect() as db:
        row = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    return _camera_view(row)


@app.patch("/api/cameras/{camera_id}")
async def update_camera(
    camera_id: str,
    payload: CameraUpdateRequest,
    request: Request,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    with connect() as db:
        cur = db.execute(
            "UPDATE cameras SET recording_mode=?, "
            "recording_status=CASE WHEN ?='motion' THEN 'unsupported' ELSE 'stopped' END, "
            "last_error=CASE WHEN ?='motion' THEN 'Motion detection is not enabled in this implementation slice' ELSE NULL END, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (payload.recording_mode, payload.recording_mode, payload.recording_mode, camera_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Camera not found")
    await request.app.state.recorders.sync_camera(camera_id)
    with connect() as db:
        row = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    return _camera_view(row)


@app.post("/api/cameras/bulk-update")
async def bulk_update_cameras(
    payload: CameraBulkUpdateRequest,
    request: Request,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    if "group_name" not in payload.model_fields_set and payload.recording_mode is None:
        raise HTTPException(status_code=422, detail="Specify a group assignment or recording mode")
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
            assignments.extend(("recording_mode=?", "recording_status=?", "last_error=?"))
            values.extend((payload.recording_mode,
                           "unsupported" if payload.recording_mode == "motion" else ("stopped" if payload.recording_mode == "disabled" else "connecting"),
                           "Motion detection is not enabled in this implementation slice" if payload.recording_mode == "motion" else None))
        assignments.append("updated_at=?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.extend(payload.camera_ids)
        db.execute(f"UPDATE cameras SET {','.join(assignments)} WHERE id IN ({','.join('?' for _ in payload.camera_ids)})", values)
    if payload.recording_mode is not None:
        await asyncio.gather(*(request.app.state.recorders.sync_camera(camera_id) for camera_id in payload.camera_ids))
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
        safe_stream_url(payload.stream_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with connect() as db:
        camera = db.execute("SELECT id,recording_mode FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    capability = await probe_stream(payload.stream_url)
    if not capability.get("reachable"):
        raise HTTPException(status_code=422, detail=str(capability.get("reason", "Updated camera stream could not be opened")))
    video = capability["video"]
    with connect() as db:
        db.execute(
            "UPDATE cameras SET stream_url_ciphertext=?,video_codec=?,video_width=?,video_height=?,last_probe_at=?,last_error=NULL,updated_at=? WHERE id=?",
            (encrypt(payload.stream_url.strip()), video["codec"], video["width"], video["height"], datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), camera_id),
        )
    await request.app.state.recorders.sync_camera(camera_id)
    with connect() as db:
        row = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    return _camera_view(row)


@app.delete("/api/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(
    camera_id: str,
    request: Request,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> Response:
    with connect() as db:
        exists = db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    await request.app.state.recorders.remove(camera_id)
    root = recordings_path()
    camera_dir = (root / camera_id).resolve()
    if camera_dir.parent == root and camera_dir.is_dir():
        try:
            shutil.rmtree(camera_dir)
        except OSError as exc:
            raise HTTPException(status_code=500, detail="Stored footage could not be removed; camera configuration was retained") from exc
    with connect() as db:
        db.execute("DELETE FROM cameras WHERE id=?", (camera_id,))
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
                "SELECT id, starts_at, ends_at, size_bytes FROM recording_segments WHERE camera_id=? ORDER BY starts_at DESC LIMIT 500",
                (camera_id,),
            ).fetchall()
        else:
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                raise HTTPException(status_code=422, detail="from and to must be timezone-aware and form a positive interval")
            rows = db.execute(
                "SELECT id, starts_at, ends_at, size_bytes FROM recording_segments "
                "WHERE camera_id=? AND starts_at<? AND ends_at>? ORDER BY starts_at",
                (camera_id, end.astimezone(timezone.utc).isoformat(), start.astimezone(timezone.utc).isoformat()),
            ).fetchall()
    segments = [
        {"id": row["id"], "starts_at": row["starts_at"], "ends_at": row["ends_at"], "size_bytes": row["size_bytes"],
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
    root = recordings_path()
    path = (root / row["relative_path"]).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=410, detail="Recorded interval is unavailable")
    return FileResponse(path, media_type="video/mp4", filename=path.name, content_disposition_type="inline")
