"""Authenticated live video and audio delivery with bounded viewer admission."""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from app.viewing import MAX_LIVE_AUDIO_LISTENERS, MAX_LIVE_TRANSCODES, MAX_LIVE_VIEWERS
from app import live_variant
from app.live_hub import LiveHub, Viewer
from app.principal import Administrator, CurrentUser, current_user, requires_scope
from app.store import connect
from app import network, privacy, viewing_audit
from app.credentials import decrypt
from app.recording import safe_stream_url, _manifest_bytes

router = APIRouter()


_live_view_lock = threading.Lock()
_live_view_count = 0
_live_audio_count = 0
_live_camera_status: dict[str, dict[str, object]] = {}
# H.265 conversions in use: stream key -> viewers. A viewer joining a conversion that is running costs nothing more; a new one needs a free place.
_transcodes: dict[tuple[str, ...], int] = {}


def live_counts() -> tuple[int, int]:
    """Live video streams and audio listeners being served right now."""
    with _live_view_lock:
        return _live_view_count, _live_audio_count


def transcode_counts(hub: LiveHub | None = None) -> tuple[int, int]:
    """Conversions of H.265 to H.264 with viewers, and the viewers they serve. Given the hub, the first number is every conversion
    running, including one that is closing because its last viewer has just left."""
    with _live_view_lock:
        watched, viewers = set(_transcodes), sum(_transcodes.values())
    return len(watched | {stream.key for stream in hub.conversions()} if hub is not None else watched), viewers


def _count_transcode(key: tuple[str, ...], limit: int) -> bool:
    with _live_view_lock:
        if key not in _transcodes and len(_transcodes) >= limit:
            return False
        _transcodes[key] = _transcodes.get(key, 0) + 1
        return True


async def _acquire_transcode(key: tuple[str, ...], hub: LiveHub) -> bool:
    """A place for a viewer of the conversion with this stream key. Joining one that is running costs nothing more; a new one needs a free
    place among ``MAX_LIVE_TRANSCODES``, counting conversions that are still running after their last viewer left. Those are closed to
    make room, so the number running never passes the limit (#344)."""
    while True:
        running = hub.conversions()
        if any(stream.key == key for stream in running) or len(running) < MAX_LIVE_TRANSCODES:
            # The place is taken in the same step as the look, with nothing awaited between: two viewers cannot both take the last one.
            return _count_transcode(key, MAX_LIVE_TRANSCODES)
        unwatched = [stream for stream in running if not stream.viewers]
        if not unwatched:
            return False
        await hub.evict(unwatched[0])


def _release_transcode(key: tuple[str, ...]) -> None:
    with _live_view_lock:
        if key in _transcodes:
            _transcodes[key] -= 1
            if _transcodes[key] <= 0:
                del _transcodes[key]


def _acquire_live_view(camera_id: str) -> bool:
    global _live_view_count
    with _live_view_lock:
        if _live_view_count >= MAX_LIVE_VIEWERS:
            return False
        _live_view_count += 1
        status_data = _live_camera_status.setdefault(camera_id, {"viewers": 0, "last_frame_at": None})
        status_data["viewers"] = int(status_data["viewers"]) + 1
        return True


def mark_camera_fresh(camera_id: str) -> None:
    """A frame of the camera's live stream just went out: what the live status reports as fresh. Streams held ready (``app/live_ready.py``) call this too, so a viewer
    who joins one finds its status current."""
    with _live_view_lock:
        status_data = _live_camera_status.get(camera_id)
        if status_data:
            status_data["last_frame_at"] = datetime.now(timezone.utc).isoformat()


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


@router.get("/api/system/live-startup")
def live_startup(request: Request, _: Administrator) -> dict[str, object]:
    """How long viewers waited for live video recently (since the server started): the summary, and the latest starts with camera names."""
    hub: LiveHub = request.app.state.live_hub
    recent = list(hub.starts)[-30:]
    with connect() as db:
        names = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM cameras")}
    return {"summary": hub.start_summary(), "recent": [{**entry, "camera": names.get(str(entry.get("camera_id")), "")} for entry in reversed(recent)]}


@router.get("/api/cameras/{camera_id}/live/status")
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


@router.get("/api/cameras/{camera_id}/audio/live")
@requires_scope("media")
def live_camera_audio(
    camera_id: str,
    user: CurrentUser,
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
        watching = viewing_audit.LiveSession(user.id, camera_id, "audio")
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
                await watching.delivered()
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
            watching.close()

    return StreamingResponse(stream(), media_type="audio/mpeg", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def _mjpeg_frames(camera_id: str, source: str, quality: str, zones: list | None = None):
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("Live video transcoding is unavailable")
    width, fps, jpeg_quality = (640, 6, 8) if quality == "low" else (1280, 12, 5)
    args = [
        executable, "-hide_banner", "-nostats", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-protocol_whitelist", "concat,pipe,rtsp,rtsps,tcp,udp,rtp,tls,crypto", "-i", "pipe:0",
        "-map", "0:v:0", "-an", "-vf", privacy.with_mask(f"fps={fps},scale={width}:-2:force_original_aspect_ratio=decrease", zones or []),
        "-q:v", str(jpeg_quality), "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
    ]
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    if process.stdin is None or process.stdout is None:
        raise RuntimeError("Live video transcoder did not start")
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


@router.get("/api/cameras/{camera_id}/live")
@requires_scope("media")
def live_camera(
    camera_id: str,
    user: CurrentUser,
    quality: Literal["low", "high"] = Query(default="low"),
    unmasked: Annotated[bool, Query()] = False,
) -> StreamingResponse:
    with connect() as db:
        camera = db.execute("SELECT stream_url_ciphertext,live_substream_url_ciphertext,archived_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    if camera["archived_at"]:
        raise HTTPException(status_code=410, detail="Camera is archived")
    try:
        zones = privacy.zones_for(user, camera_id, unmasked=unmasked)
    except privacy.UnmaskedRefused as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    guard = privacy.StreamGuard(user, camera_id, unmasked)
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
        watching = viewing_audit.LiveSession(user.id, camera_id, quality)
        try:
            async for frame in _mjpeg_frames(camera_id, source, quality, zones):
                if not await guard.check():
                    return  # Masks or grants changed: the viewer reconnects under the new ones.
                await watching.delivered()
                yield frame
        except (OSError, RuntimeError, asyncio.TimeoutError):
            return
        finally:
            _release_live_view(camera_id)
            watching.close()

    return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


HEVC_NAMES = live_variant.HEVC_NAMES

# Close codes for the live WebSocket. 44xx mirror the HTTP status they stand for.
CLOSE_UNAUTHORIZED, CLOSE_FORBIDDEN, CLOSE_NOT_FOUND, CLOSE_ARCHIVED = 4401, 4403, 4404, 4410
CLOSE_UNSUPPORTED, CLOSE_BUSY, CLOSE_TOO_SLOW, CLOSE_STREAM_FAILED = 4415, 4429, 4408, 4502


def _same_origin(websocket: WebSocket) -> bool:
    """Refuse cross-site WebSocket handshakes; non-browser clients send no Origin. Behind a trusted proxy the host is the one the proxy reports."""
    origin = websocket.headers.get("origin")
    return True if origin is None else network.origin_allowed(websocket, origin)


def _client_message(message: dict[str, object]) -> dict[str, object]:
    try:
        parsed = json.loads(message.get("text") or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _refuse(websocket: WebSocket, code: int, message: dict[str, object]) -> None:
    await websocket.send_json(message)
    await websocket.close(code=code)


async def _ended(websocket: WebSocket, reason: str | None) -> None:
    """An open stream whose credential or camera access ended (#298): the person is told which, and the socket closes."""
    if reason == "credential":
        await _refuse(websocket, CLOSE_UNAUTHORIZED, {"type": "unauthorized", "detail": "Your sign-in has ended"})
    else:
        await _refuse(websocket, CLOSE_FORBIDDEN, {"type": "forbidden", "detail": "Your access to this camera has ended"})


@dataclass(frozen=True)
class _Refusal:
    code: int
    message: dict[str, object]


def _admit(websocket: WebSocket, camera_id: str, unmasked: bool) -> "_Refusal | tuple[Any, Any, list | None, privacy.StreamGuard]":
    """Everything decided from the database when a viewer connects: who they are, the camera, its masks for them, and the guard that
    keeps checking. Blocking; the handler runs it in a thread."""
    try:
        principal = current_user(websocket)
    except HTTPException as exc:
        if exc.status_code == 404:  # A camera this person may not see, refused exactly as one that does not exist.
            return _Refusal(CLOSE_NOT_FOUND, {"type": "not_found", "detail": "Camera not found"})
        code = CLOSE_UNAUTHORIZED if exc.status_code == 401 else CLOSE_FORBIDDEN
        return _Refusal(code, {"type": "unauthorized" if code == CLOSE_UNAUTHORIZED else "forbidden", "detail": exc.detail})
    with connect() as db:
        camera = db.execute(
            "SELECT stream_url_ciphertext,live_substream_url_ciphertext,video_codec,audio_enabled,archived_at FROM cameras WHERE id=?",
            (camera_id,),
        ).fetchone()
    if camera is None:
        return _Refusal(CLOSE_NOT_FOUND, {"type": "not_found", "detail": "Camera not found"})
    if camera["archived_at"]:
        return _Refusal(CLOSE_ARCHIVED, {"type": "archived", "detail": "Camera is archived"})
    try:
        zones = privacy.zones_for(principal, camera_id, unmasked=unmasked)
    except privacy.UnmaskedRefused as exc:
        return _Refusal(CLOSE_FORBIDDEN, {"type": "forbidden", "detail": str(exc)})
    return principal, camera, zones, privacy.StreamGuard(principal, camera_id, unmasked)


@router.websocket("/api/cameras/{camera_id}/live/stream")
@requires_scope("media")
async def live_stream(
    websocket: WebSocket,
    camera_id: str,
    quality: Literal["low", "high"] = "low",
    audio: bool = False,
    unmasked: bool = False,
    hevc: bool = False,
) -> None:
    """Fragmented MP4 live video for Media Source Extensions.

    ``hevc`` says the browser can play H.265: an H.265 camera is then passed through as it is; otherwise it is converted to H.264 here, once for all of
    its viewers (``init`` says ``transcoded``). The MJPEG ``fallback`` is only for when that fails too.

    Messages: a JSON ``init`` (MIME/codecs, profile, audio), the binary
    initialization segment, then binary media fragments, starting at a keyframe.
    The client may send ``{"type": "listen", "on": true|false}`` to claim or return
    an audio slot; the reply is ``listening``. Refusals send a JSON message with
    ``type`` and close with a 44xx/45xx code;
    ``unsupported`` names the MJPEG ``fallback`` URL for codecs the browser cannot
    play directly.
    """
    if not _same_origin(websocket):
        await websocket.close(code=1008)
        return
    asked_at = time.monotonic()
    await websocket.accept()
    # Who is asking, the camera and its masks are database reads (and, for an API token, a write): in a thread, so that a viewer
    # connecting while the database is busy does not stop every other viewer's video (#355).
    admitted = await asyncio.to_thread(_admit, websocket, camera_id, unmasked)
    if isinstance(admitted, _Refusal):
        await _refuse(websocket, admitted.code, admitted.message)
        return
    principal, camera, zones, guard = admitted
    fallback = f"/api/cameras/{camera_id}/live?quality={quality}" + ("&unmasked=true" if unmasked else "")
    # A masked camera is transcoded with the mask drawn in (H.264, so any camera codec plays).
    variant = live_variant.choose(camera, zones, quality, hevc)
    video_filter, profile, codec, camera_is_hevc = variant.video_filter, variant.profile, variant.codec, variant.camera_is_hevc
    hevc_copy, transcode = variant.hevc_copy, variant.transcode
    if not video_filter and profile == "main" and codec and codec not in {"h264", "avc", "avc1"} and not camera_is_hevc:
        await _refuse(websocket, CLOSE_UNSUPPORTED, {"type": "unsupported", "codec": codec, "fallback": fallback,
                                                     "detail": f"{codec.upper()} video cannot be played directly; use the fallback stream"})
        return
    try:
        source = decrypt(camera["live_substream_url_ciphertext"] if profile == "sub" else camera["stream_url_ciphertext"])
        safe_stream_url(source)
    except (RuntimeError, ValueError):
        await _refuse(websocket, CLOSE_STREAM_FAILED, {"type": "error", "detail": "Camera stream configuration is invalid"})
        return
    if not _acquire_live_view(camera_id):
        await _refuse(websocket, CLOSE_BUSY, {"type": "busy", "detail": "All live-view slots are in use; close another view and retry"})
        return
    include_audio = variant.include_audio
    hub: LiveHub = websocket.app.state.live_hub
    stream_key = conversion_key = live_variant.key_of(camera_id, variant, source)  # A conversion is counted by the stream that does it
    if transcode and not await _acquire_transcode(conversion_key, hub):
        _release_live_view(camera_id)
        await _refuse(websocket, CLOSE_BUSY, {"type": "busy", "detail": "All video conversion slots are in use; close another converted view and retry"})
        return
    listening = audio and include_audio and _acquire_live_audio()
    viewer = Viewer()

    def mark_fresh() -> None:
        mark_camera_fresh(camera_id)

    joined_running = (running := hub.streams.get(stream_key)) is not None and running.ready.is_set() and not running.failed
    stream = hub.join(stream_key, source, include_audio, viewer, mark_fresh, video_filter, transcode, hevc_copy)
    mode = variant.mode
    wait: dict[str, object] = {"camera_id": camera_id, "at": datetime.now(timezone.utc).isoformat(), "profile": profile, "mode": mode, "joined_running": joined_running}
    receiver = asyncio.create_task(websocket.receive())
    getter: asyncio.Task | None = None
    watching = viewing_audit.LiveSession(principal.id, camera_id, quality)
    try:
        await asyncio.wait({receiver, asyncio.create_task(stream.ready.wait())}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
        if receiver.done():
            return
        if not stream.ready.is_set() or stream.failed:
            if transcode or hevc_copy:
                # H.265 could not be passed on or converted: only now is the compatibility stream offered.
                await _refuse(websocket, CLOSE_UNSUPPORTED, {"type": "unsupported", "codec": codec, "fallback": fallback, "converted": transcode,
                                                             "detail": "This camera's H.265 video could not be prepared for the browser; use the fallback stream"})
                return
            await _refuse(websocket, CLOSE_STREAM_FAILED, {"type": "error", "detail": stream.failed or "The camera did not start streaming in time"})
            return
        wanted = ("hvc1", "hev1") if hevc_copy else ("avc1",)
        if not any(name in (stream.mime or "") for name in wanted):
            await _refuse(websocket, CLOSE_UNSUPPORTED, {"type": "unsupported", "codec": "unknown", "fallback": fallback,
                                                         "detail": "This camera's video cannot be played directly; use the fallback stream"})
            return
        # Audio is offered only when the stream really carries a track.
        has_audio = "mp4a" in (stream.mime or "")
        if listening and not has_audio:
            _release_live_audio()
            listening = False
        include_audio = has_audio
        await websocket.send_json({"type": "init", "mime": stream.mime, "profile": profile, "audio": has_audio, "listening": listening,
                                   "codec": "hevc" if hevc_copy else "h264", "transcoded": transcode, "accelerated": bool(stream.accelerated)})
        await websocket.send_bytes(stream.init)
        wait["init_ms"] = round((time.monotonic() - asked_at) * 1000)
        while True:
            await watching.delivered()
            if getter is None:
                getter = asyncio.create_task(viewer.queue.get())
            # Wake every second even when the camera is quiet, so a viewer whose access ended is cut off at once.
            done, _ = await asyncio.wait({getter, receiver}, timeout=privacy.ACCESS_RECHECK_SECONDS, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                if not await guard.check() and guard.reason in {"access", "credential"}:
                    await _ended(websocket, guard.reason)
                    return
                continue
            if receiver in done:
                message = receiver.result()
                if message["type"] == "websocket.disconnect":
                    return
                request = _client_message(message)
                if request.get("type") == "listen":
                    # Unmuting claims one of the audio slots; muting returns it.
                    wanted = bool(request.get("on"))
                    if wanted and not listening:
                        listening = include_audio and _acquire_live_audio()
                    elif not wanted and listening:
                        _release_live_audio()
                        listening = False
                    reason = None if listening or not wanted else ("This camera's audio is not enabled" if not include_audio else "All live audio slots are in use")
                    await websocket.send_json({"type": "listening", "on": listening, "detail": reason})
                receiver = asyncio.create_task(websocket.receive())
                continue
            item = getter.result()
            getter = None
            if not await guard.check():
                if guard.reason in {"access", "credential"}:
                    await _ended(websocket, guard.reason)
                else:
                    await _refuse(websocket, CLOSE_STREAM_FAILED, {"type": "error", "detail": "This camera's privacy masks changed; reconnecting applies them"})
                return
            if item is None:
                if viewer.closed_reason == "too_slow":
                    await _refuse(websocket, CLOSE_TOO_SLOW, {"type": "too_slow", "detail": "Video could not be delivered fast enough; reconnect to resume at live"})
                else:
                    await _refuse(websocket, CLOSE_STREAM_FAILED, {"type": "error", "detail": stream.failed or "The camera stream ended"})
                return
            await websocket.send_bytes(item)
            if "first_media_ms" not in wait:
                # The first picture left the server: how long this viewer waited, and, for the viewer who started the camera's stream, where the time went.
                wait["first_media_ms"] = round((time.monotonic() - asked_at) * 1000)
                if not joined_running:
                    wait.update(getattr(stream, "timing", None) or {})
                hub.record_start(wait)  # Measuring never gets in the way of the picture
    except (WebSocketDisconnect, RuntimeError, OSError):
        return
    finally:
        receiver.cancel()
        if getter is not None:
            getter.cancel()
        hub.leave(stream, viewer)
        if listening:
            _release_live_audio()
        if transcode:
            _release_transcode(conversion_key)
        _release_live_view(camera_id)
        watching.close()
