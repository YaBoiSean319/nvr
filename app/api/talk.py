"""Two-way audio (#147): talk through a camera's speaker from Live.

While someone holds the talk control, the browser sends the microphone over this WebSocket and the server forwards it to the camera over the ONVIF audio
backchannel (``app/talk.py``). One person talks through a camera at a time. Administrators turn talking on per camera and choose the roles that may use it, and
whether the audio is kept. Every talk is in the audit log and in the camera's talk history; the audio is stored only when the camera's setting says so.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app import talk
from app.api import live_media
from app.audit import record
from app.credentials import decrypt
from app.principal import Administrator, CurrentUser, Principal, current_user, requires_scope
from app.recording import safe_stream_url
from app.store import connect

router = APIRouter()

SETTINGS_RECHECK_SECONDS = 2.0  # How often a talk in progress looks at whether it is still allowed


class TalkSettingsRequest(BaseModel):
    enabled: bool
    roles: list[Literal["administrator", "operator"]] = Field(default_factory=lambda: ["administrator"], max_length=len(talk.ROLES))
    record_audio: bool = False


def _camera_row(camera_id: str, user: Principal):
    with connect() as db:
        return _visible_camera(db, camera_id, user), talk.settings(db, camera_id)


def _visible_camera(db, camera_id: str, user: Principal):
    row = db.execute("SELECT id,name,archived_at,stream_url_ciphertext FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if row is None or row["archived_at"] or not user.access.can_see(camera_id):
        raise HTTPException(status_code=404, detail="Camera not found")
    return row


def _view(user: Principal, config: dict[str, object], camera_id: str) -> dict[str, object]:
    out: dict[str, object] = {"enabled": config["enabled"], "allowed": _may(user, config), "talking": talk.talkers.holder(camera_id)}
    if user.is_administrator:
        out |= {"roles": config["roles"], "record_audio": config["record_audio"]}
    return out


def _may(user: Principal, config: dict[str, object]) -> bool:
    # A video wall only watches, and an API token acts for a program, not a person at a microphone.
    return not user.wall and user.scopes is None and talk.allowed(config, user.role)


@router.get("/api/talk")
def talk_cameras(user: CurrentUser) -> dict[str, object]:
    """The cameras this person may talk through, and who is talking through each now."""
    holders = talk.talkers.holders()
    with connect() as db:
        rows = db.execute("SELECT t.camera_id FROM camera_talk t JOIN cameras c ON c.id=t.camera_id WHERE t.enabled=1 AND c.archived_at IS NULL").fetchall()
        cameras = {}
        for row in rows:
            camera_id = row["camera_id"]
            if user.access.can_see(camera_id) and _may(user, talk.settings(db, camera_id)):
                cameras[camera_id] = {"talking": holders.get(camera_id)}
    return {"cameras": cameras}


@router.get("/api/cameras/{camera_id}/talk")
def get_talk(camera_id: str, user: CurrentUser) -> dict[str, object]:
    _, config = _camera_row(camera_id, user)
    return _view(user, config, camera_id)


@router.put("/api/cameras/{camera_id}/talk")
def put_talk(camera_id: str, payload: TalkSettingsRequest, user: Administrator) -> dict[str, object]:
    roles = [role for role in talk.ROLES if role in payload.roles]
    if payload.enabled and not roles:
        raise HTTPException(status_code=422, detail="Choose at least one role that may talk")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _visible_camera(db, camera_id, user)
            db.execute("INSERT INTO camera_talk(camera_id,enabled,roles_json,record_audio) VALUES(?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET "
                       "enabled=excluded.enabled,roles_json=excluded.roles_json,record_audio=excluded.record_audio,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                       (camera_id, int(payload.enabled), json.dumps(roles), int(payload.record_audio)))
            record(db, user.id, "camera_talk_changed", "camera", camera_id, enabled=payload.enabled, roles=roles, record_audio=payload.record_audio)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        config = talk.settings(db, camera_id)
    return _view(user, config, camera_id)


@router.get("/api/cameras/{camera_id}/talk/sessions")
def talk_sessions(camera_id: str, user: Administrator) -> dict[str, object]:
    _camera_row(camera_id, user)
    with connect() as db:
        rows = db.execute("SELECT id,username,started_at,ended_at,seconds,ended_reason,codec,audio_wav IS NOT NULL AS has_audio FROM talk_sessions WHERE camera_id=? "
                          "ORDER BY started_at DESC LIMIT 50", (camera_id,)).fetchall()
    return {"sessions": [{**{key: row[key] for key in row.keys() if key != "has_audio"}, "has_audio": bool(row["has_audio"])} for row in rows]}


@router.get("/api/cameras/{camera_id}/talk/sessions/{session_id}/audio")
def talk_audio(camera_id: str, session_id: str, user: Administrator) -> Response:
    _camera_row(camera_id, user)
    with connect() as db:
        row = db.execute("SELECT audio_wav FROM talk_sessions WHERE id=? AND camera_id=?", (session_id, camera_id)).fetchone()
        if row is None or row["audio_wav"] is None:
            raise HTTPException(status_code=404, detail="No audio was kept for this talk")
        record(db, user.id, "talk_audio_downloaded", "camera", camera_id, session=session_id)
    return Response(content=bytes(row["audio_wav"]), media_type="audio/wav", headers={"Content-Disposition": f'attachment; filename="talk-{session_id[:8]}.wav"'})


def _still_allowed(camera_id: str, user: Principal) -> bool:
    try:
        _, config = _camera_row(camera_id, user)
    except HTTPException:
        return False
    return _may(user, config)


# Close codes follow live_media's: 4403 not allowed, 4404 no such camera, 4415 camera has no usable speaker, 4429 someone else is talking, 4502 the camera failed.
@router.websocket("/api/cameras/{camera_id}/talk/stream")
@requires_scope("media")
async def talk_stream(websocket: WebSocket, camera_id: str) -> None:
    """Send the microphone to the camera's speaker.

    Binary messages are 16-bit little-endian mono PCM at 8 kHz. The server answers ``{"type": "ready", "codec"}`` once the camera is listening, ``busy`` (with the
    ``talker``) if someone else is talking, and ``ended`` (with a ``reason``) as it closes. The client may send ``{"type": "stop"}``; closing the socket stops too.
    """
    if not live_media._same_origin(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    refuse = live_media._refuse
    try:
        principal = current_user(websocket)
    except HTTPException as exc:
        if exc.status_code == 404:
            await refuse(websocket, live_media.CLOSE_NOT_FOUND, {"type": "not_found", "detail": "Camera not found"})
        else:
            code = live_media.CLOSE_UNAUTHORIZED if exc.status_code == 401 else live_media.CLOSE_FORBIDDEN
            await refuse(websocket, code, {"type": "unauthorized" if code == live_media.CLOSE_UNAUTHORIZED else "forbidden", "detail": exc.detail})
        return
    try:
        row, config = await asyncio.to_thread(_camera_row, camera_id, principal)
    except HTTPException:
        await refuse(websocket, live_media.CLOSE_NOT_FOUND, {"type": "not_found", "detail": "Camera not found"})
        return
    if not _may(principal, config):
        await refuse(websocket, live_media.CLOSE_FORBIDDEN, {"type": "forbidden", "detail": "Talking is not turned on for you on this camera"})
        return
    try:
        talking = talk.talkers.begin(camera_id, principal.id, principal.username)
    except talk.TalkBusy as busy:
        await refuse(websocket, live_media.CLOSE_BUSY, {"type": "busy", "talker": busy.holder, "detail": f"{busy.holder} is talking through this camera"})
        return
    backchannel: talk.RtspBackchannel | None = None
    reason, recorded, started, codec = "released", False, False, None
    session: talk.Talking | None = None
    try:
        # Every attempt that gets this far is in the talk history and the audit log, whether or not the camera could be reached.
        await asyncio.to_thread(talk.begin_record, talking)
        started = True
        try:
            url = decrypt(row["stream_url_ciphertext"])
            safe_stream_url(url)
            backchannel = talk.RtspBackchannel(url)
            track = await backchannel.connect()
            codec = track.codec
        except talk.BackchannelUnsupported as exc:
            reason = "unsupported"
            await refuse(websocket, live_media.CLOSE_UNSUPPORTED, {"type": "unsupported", "detail": str(exc)})
            return
        except (talk.BackchannelError, RuntimeError, ValueError) as exc:
            reason = "camera_failed"
            await refuse(websocket, live_media.CLOSE_STREAM_FAILED, {"type": "error", "detail": str(exc) if isinstance(exc, talk.BackchannelError) else "Camera stream configuration is invalid"})
            return
        session = talk.Talking(backchannel, bool(config["record_audio"]))
        await websocket.send_json({"type": "ready", "codec": track.codec, "max_seconds": talk.MAX_TALK_SECONDS})
        last_audio = last_check = time.monotonic()
        while True:
            now = time.monotonic()
            if now - talking.started > talk.MAX_TALK_SECONDS:
                reason = "limit"
                break
            if backchannel.closed.is_set():
                reason = "camera_failed"
                break
            if now - last_check >= SETTINGS_RECHECK_SECONDS:
                last_check = now
                if not await asyncio.to_thread(_still_allowed, camera_id, principal):  # Turned off, or the role removed, while someone is talking
                    reason = "turned_off"
                    break
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=1.0)
            except asyncio.TimeoutError:
                if time.monotonic() - last_audio > talk.IDLE_SECONDS:
                    reason = "idle"
                    break
                continue
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes"):
                last_audio = time.monotonic()
                try:
                    await session.feed(message["bytes"])
                except ValueError:
                    reason = "bad_audio"
                    break
                except talk.BackchannelError:
                    reason = "camera_failed"
                    break
            elif message.get("text"):
                with contextlib.suppress(ValueError):
                    if json.loads(message["text"]).get("type") == "stop":
                        break
    except (WebSocketDisconnect, RuntimeError):  # The browser went away (a send on a closed socket raises RuntimeError)
        pass
    finally:
        talk.talkers.end(talking)  # First, and without waiting: a talk that is over never holds the camera, even if this task is being cancelled
        if backchannel is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(backchannel.close())
        if started:
            kept = bytes(session.keep) if session is not None and session.keep else None
            recorded = bool(kept)
            await asyncio.shield(asyncio.to_thread(talk.end_record, talking, reason, kept, codec))
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "ended", "reason": reason, "recorded": recorded})
            await websocket.close()
