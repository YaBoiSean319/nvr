"""Authenticated live video and audio delivery with bounded viewer admission."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import threading
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from app.viewing import MAX_LIVE_AUDIO_LISTENERS, MAX_LIVE_VIEWERS
from app.live_hub import LiveHub, Viewer
from app.principal import CurrentUser, current_user, requires_scope
from app.store import connect
from app import privacy
from app.credentials import decrypt
from app.recording import safe_stream_url, _manifest_bytes

router = APIRouter()


_live_view_lock = threading.Lock()
_live_view_count = 0
_live_audio_count = 0
_live_camera_status: dict[str, dict[str, object]] = {}


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
    from app.recording import _manifest_bytes
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
    if not shutil.which("ffmpeg"):
        raise HTTPException(status_code=503, detail="Live video transcoding is unavailable")
    try:
        zones = privacy.zones_for(user, camera_id, unmasked=unmasked)
    except privacy.UnmaskedRefused as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
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
            async for frame in _mjpeg_frames(camera_id, source, quality, zones):
                yield frame
        except (OSError, RuntimeError, asyncio.TimeoutError):
            return
        finally:
            _release_live_view(camera_id)

    return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# Close codes for the live WebSocket. 44xx mirror the HTTP status they stand for.
CLOSE_UNAUTHORIZED, CLOSE_FORBIDDEN, CLOSE_NOT_FOUND, CLOSE_ARCHIVED = 4401, 4403, 4404, 4410
CLOSE_UNSUPPORTED, CLOSE_BUSY, CLOSE_TOO_SLOW, CLOSE_STREAM_FAILED = 4415, 4429, 4408, 4502


def _same_origin(websocket: WebSocket) -> bool:
    """Refuse cross-site WebSocket handshakes; non-browser clients send no Origin."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    host = websocket.headers.get("host", "")
    return urlsplit(origin).netloc.lower() == host.lower()


def _client_message(message: dict[str, object]) -> dict[str, object]:
    try:
        parsed = json.loads(message.get("text") or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _refuse(websocket: WebSocket, code: int, message: dict[str, object]) -> None:
    await websocket.send_json(message)
    await websocket.close(code=code)


@router.websocket("/api/cameras/{camera_id}/live/stream")
@requires_scope("media")
async def live_stream(
    websocket: WebSocket,
    camera_id: str,
    quality: Literal["low", "high"] = "low",
    audio: bool = False,
    unmasked: bool = False,
) -> None:
    """Fragmented MP4 live video for Media Source Extensions.

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
    await websocket.accept()
    try:
        principal = current_user(websocket)
    except HTTPException as exc:
        code = CLOSE_UNAUTHORIZED if exc.status_code == 401 else CLOSE_FORBIDDEN
        await _refuse(websocket, code, {"type": "unauthorized" if code == CLOSE_UNAUTHORIZED else "forbidden", "detail": exc.detail})
        return
    with connect() as db:
        camera = db.execute(
            "SELECT stream_url_ciphertext,live_substream_url_ciphertext,video_codec,audio_enabled,archived_at FROM cameras WHERE id=?",
            (camera_id,),
        ).fetchone()
    fallback = f"/api/cameras/{camera_id}/live?quality={quality}" + ("&unmasked=true" if unmasked else "")
    if camera is None:
        await _refuse(websocket, CLOSE_NOT_FOUND, {"type": "not_found", "detail": "Camera not found"})
        return
    if camera["archived_at"]:
        await _refuse(websocket, CLOSE_ARCHIVED, {"type": "archived", "detail": "Camera is archived"})
        return
    try:
        zones = privacy.zones_for(principal, camera_id, unmasked=unmasked)
    except privacy.UnmaskedRefused as exc:
        await _refuse(websocket, CLOSE_FORBIDDEN, {"type": "forbidden", "detail": str(exc)})
        return
    # A masked camera is transcoded with the mask drawn in (H.264, so any camera codec plays).
    video_filter = privacy.drawbox_filter(zones) if zones else None
    profile = "sub" if quality == "low" and camera["live_substream_url_ciphertext"] else "main"
    codec = (camera["video_codec"] or "").lower()
    if not video_filter and profile == "main" and codec and codec not in {"h264", "avc", "avc1"}:
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
    include_audio = bool(camera["audio_enabled"])
    listening = audio and include_audio and _acquire_live_audio()
    hub: LiveHub = websocket.app.state.live_hub
    viewer = Viewer()

    def mark_fresh() -> None:
        with _live_view_lock:
            status_data = _live_camera_status.get(camera_id)
            if status_data:
                status_data["last_frame_at"] = datetime.now(timezone.utc).isoformat()

    # The source fingerprint keeps a changed stream URL (rotation, new profile) from
    # joining a stream that is still connected to the old one.
    fingerprint = hashlib.sha256(f"{source}|{video_filter or ''}".encode()).hexdigest()[:16]
    stream = hub.join((camera_id, profile, "audio" if include_audio else "video", "masked" if video_filter else "direct", fingerprint),
                      source, include_audio, viewer, mark_fresh, video_filter)
    receiver = asyncio.create_task(websocket.receive())
    try:
        await asyncio.wait({receiver, asyncio.create_task(stream.ready.wait())}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
        if receiver.done():
            return
        if not stream.ready.is_set() or stream.failed:
            await _refuse(websocket, CLOSE_STREAM_FAILED, {"type": "error", "detail": stream.failed or "The camera did not start streaming in time"})
            return
        if "avc1" not in (stream.mime or ""):
            await _refuse(websocket, CLOSE_UNSUPPORTED, {"type": "unsupported", "codec": "unknown", "fallback": fallback,
                                                         "detail": "This camera's video cannot be played directly; use the fallback stream"})
            return
        # Audio is offered only when the stream really carries a track.
        has_audio = "mp4a" in (stream.mime or "")
        if listening and not has_audio:
            _release_live_audio()
            listening = False
        include_audio = has_audio
        await websocket.send_json({"type": "init", "mime": stream.mime, "profile": profile, "audio": has_audio, "listening": listening})
        await websocket.send_bytes(stream.init)
        while True:
            getter = asyncio.create_task(viewer.queue.get())
            done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                getter.cancel()
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
            if item is None:
                if viewer.closed_reason == "too_slow":
                    await _refuse(websocket, CLOSE_TOO_SLOW, {"type": "too_slow", "detail": "Video could not be delivered fast enough; reconnect to resume at live"})
                else:
                    await _refuse(websocket, CLOSE_STREAM_FAILED, {"type": "error", "detail": stream.failed or "The camera stream ended"})
                return
            await websocket.send_bytes(item)
    except (WebSocketDisconnect, RuntimeError, OSError):
        return
    finally:
        receiver.cancel()
        hub.leave(stream, viewer)
        if listening:
            _release_live_audio()
        _release_live_view(camera_id)
