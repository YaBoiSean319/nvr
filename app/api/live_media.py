"""Authenticated live video and audio delivery with bounded viewer admission."""
from __future__ import annotations

import asyncio
import shutil
import threading
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.viewing import MAX_LIVE_AUDIO_LISTENERS, MAX_LIVE_VIEWERS
from app.principal import CurrentUser
from app.store import connect
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
