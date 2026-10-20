from __future__ import annotations

import os
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .security import hash_password, new_session_token, token_digest, verify_password
from .store import connect, database_path, initialize
from .credentials import encrypt, decrypt
from .recording import RecordingManager, masked_stream_url, probe_stream, recordings_path, safe_stream_url

APP_VERSION = "0.1.0"
COOKIE_NAME = "nvr_session"
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.@-]{3,128}$")


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.umask(0o077)
    initialize()
    app.state.recorders = RecordingManager()
    await app.state.recorders.start()
    yield
    await app.state.recorders.stop()


app = FastAPI(title="Workplace NVR", version=APP_VERSION, lifespan=lifespan)


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


class CameraUpdateRequest(BaseModel):
    recording_mode: Literal["motion", "continuous", "disabled"]


def _session_hours() -> int:
    try:
        return min(max(int(os.environ.get("NVR_SESSION_HOURS", "12")), 1), 168)
    except ValueError:
        return 12


def _secure_cookie() -> bool:
    return os.environ.get("NVR_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}


def _new_session(response: Response, user_id: int) -> None:
    raw = new_session_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=_session_hours())
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (datetime.now(timezone.utc).isoformat(),))
        db.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_digest(raw), user_id, expires.isoformat()),
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
    response.delete_cookie(COOKIE_NAME, path="/", secure=_secure_cookie(), httponly=True, samesite="strict")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@app.get("/api/auth/me")
def me(user: Annotated[sqlite3.Row, Depends(current_user)]) -> dict[str, str]:
    return {"username": user["username"], "role": user["role"]}


@app.get("/api/system/status")
def system_status(user: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    db_path = database_path()
    with connect() as db:
        user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
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
        "configured_administrators": user_count,
        "configured_cameras": camera_count,
        "storage": storage,
    }


def _camera_view(row: sqlite3.Row) -> dict[str, object]:
    try:
        visible_url = masked_stream_url(decrypt(row["stream_url_ciphertext"]))
    except RuntimeError:
        visible_url = "rtsp://credentials-unavailable"
    return {
        "id": row["id"],
        "name": row["name"],
        "stream_url": visible_url,
        "enabled": bool(row["enabled"]),
        "recording_mode": row["recording_mode"],
        "recording_status": row["recording_status"],
        "video": {"codec": row["video_codec"], "width": row["video_width"], "height": row["video_height"]},
        "last_probe_at": row["last_probe_at"],
        "last_error": row["last_error"],
    }


@app.post("/api/cameras/test")
async def test_camera_stream(
    payload: CameraProbeRequest,
    _: Annotated[sqlite3.Row, Depends(administrator)],
) -> dict[str, object]:
    try:
        safe_stream_url(payload.stream_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await probe_stream(payload.stream_url)


@app.get("/api/cameras")
def list_cameras(_: Annotated[sqlite3.Row, Depends(administrator)]) -> dict[str, object]:
    with connect() as db:
        rows = db.execute("SELECT * FROM cameras ORDER BY name COLLATE NOCASE").fetchall()
    return {"cameras": [_camera_view(row) for row in rows]}


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
    video = capability["video"]
    camera_id = str(uuid.uuid4())
    with connect() as db:
        try:
            db.execute(
                "INSERT INTO cameras(id, name, stream_url_ciphertext, recording_mode, recording_status, last_error, video_codec, video_width, video_height, last_probe_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (camera_id, name, encrypt(payload.stream_url.strip()), payload.recording_mode,
                 "unsupported" if payload.recording_mode == "motion" else "stopped",
                 "Motion detection is not enabled in this implementation slice" if payload.recording_mode == "motion" else None,
                 video["codec"], video["width"], video["height"]),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="A camera with that name already exists") from exc
    if payload.recording_mode == "continuous":
        request.app.state.recorders.ensure(camera_id)
    with connect() as db:
        row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
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
        row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
    return _camera_view(row)


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
