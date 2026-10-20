from __future__ import annotations

import asyncio
import csv
import contextlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .credentials import decrypt
from .store import connect

SEGMENT_SECONDS = max(10, min(int(os.environ.get("NVR_SEGMENT_SECONDS", "60")), 3600))
RECONNECT_MAX_SECONDS = 30


def recordings_path() -> Path:
    return Path(os.environ.get("NVR_RECORDINGS_PATH", "/recordings")).resolve()


def safe_stream_url(raw: str) -> str:
    """Validate the only supported ingest protocols and remove userinfo/query from display."""
    parsed = urlsplit(raw.strip())
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise ValueError("Only RTSP and RTSPS camera streams are supported")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid camera stream port")
    if any(ch.isspace() or ch in "'\\\x00" for ch in raw):
        raise ValueError("Camera URL must be URL-encoded and contain no whitespace or quotes")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _manifest_bytes(source: str) -> bytes:
    # Feed the source URL to FFmpeg over stdin so camera credentials never appear in argv.
    return f"ffconcat version 1.0\nfile {source}\n".encode("utf-8")


_PROTOCOLS = "concat,pipe,rtsp,rtsps,tcp,udp,rtp,tls,crypto"


def masked_stream_url(raw: str) -> str:
    parsed = urlsplit(raw)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, "", "", ""))


async def probe_stream(raw: str, timeout_seconds: float = 8.0) -> dict[str, object]:
    source = safe_stream_url(raw)
    executable = shutil.which("ffprobe")
    if not executable:
        return {"reachable": False, "reason": "Media probe is unavailable"}
    args = [
        executable, "-v", "error", "-f", "concat", "-safe", "0", "-protocol_whitelist", _PROTOCOLS,
        "-rtsp_transport", "tcp", "-rw_timeout", "5000000", "-show_entries",
        "stream=codec_type,codec_name,width,height", "-of", "json", "pipe:0",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(_manifest_bytes(source)), timeout=timeout_seconds)
    except (asyncio.TimeoutError, OSError):
        if "proc" in locals() and proc.returncode is None:
            proc.kill()
            await proc.wait()
        return {"reachable": False, "reason": "Camera probe timed out or could not connect"}
    if proc.returncode != 0:
        return {"reachable": False, "reason": "Camera stream could not be opened"}
    try:
        streams = json.loads(stdout).get("streams", [])
    except (json.JSONDecodeError, AttributeError):
        return {"reachable": False, "reason": "Camera returned invalid stream metadata"}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        return {"reachable": False, "reason": "Camera stream has no video track"}
    return {
        "reachable": True,
        "video": {
            "codec": str(video.get("codec_name", "unknown")),
            "width": video.get("width"),
            "height": video.get("height"),
        },
    }


class RecordingManager:
    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.stopping = False

    async def start(self) -> None:
        self.stopping = False
        with connect() as db:
            rows = db.execute("SELECT id FROM cameras WHERE enabled=1 AND recording_mode='continuous'").fetchall()
        for row in rows:
            self.ensure(row["id"])

    async def stop(self) -> None:
        self.stopping = True
        for task in list(self.tasks.values()):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()

    def ensure(self, camera_id: str) -> None:
        task = self.tasks.get(camera_id)
        if task is None or task.done():
            self.tasks[camera_id] = asyncio.create_task(self._run(camera_id), name=f"record-{camera_id}")

    async def remove(self, camera_id: str) -> None:
        task = self.tasks.pop(camera_id, None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def sync_camera(self, camera_id: str) -> None:
        await self.remove(camera_id)
        with connect() as db:
            row = db.execute("SELECT enabled, recording_mode FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if row and row["enabled"] and row["recording_mode"] == "continuous" and not self.stopping:
            self.ensure(camera_id)

    def _camera(self, camera_id: str) -> sqlite3.Row | None:
        with connect() as db:
            return db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()

    def _set_status(self, camera_id: str, value: str, error: str | None = None) -> None:
        with connect() as db:
            db.execute(
                "UPDATE cameras SET recording_status=?, last_error=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (value, error, camera_id),
            )

    def _index_csv(self, camera_id: str, index_path: Path, output_dir: Path) -> int:
        indexed = 0
        try:
            with index_path.open(newline="", encoding="utf-8") as source:
                rows = list(csv.reader(source))
        except (OSError, UnicodeError, csv.Error):
            return 0
        root = recordings_path()
        for row in rows:
            if len(row) < 3:
                continue
            candidate = Path(row[0])
            if candidate.is_absolute() or candidate.name != row[0] or candidate.suffix.lower() != ".mp4":
                continue
            media = (output_dir / candidate.name).resolve()
            if root not in media.parents or not media.is_file() or media.stat().st_size < 128:
                continue
            try:
                duration = max(0.0, float(row[2]) - float(row[1]))
                started = datetime.strptime(candidate.stem, "segment-%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except (ValueError, OverflowError):
                continue
            relative = media.relative_to(root).as_posix()
            try:
                with connect() as db:
                    cur = db.execute(
                        "INSERT OR IGNORE INTO recording_segments(camera_id, relative_path, starts_at, ends_at, size_bytes) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (camera_id, relative, started.isoformat(), (started + timedelta(seconds=duration)).isoformat(), media.stat().st_size),
                    )
                    indexed += cur.rowcount
            except sqlite3.Error:
                continue
        return indexed

    async def _run(self, camera_id: str) -> None:
        executable = shutil.which("ffmpeg")
        if not executable:
            self._set_status(camera_id, "unavailable", "FFmpeg is not installed")
            return
        failures = 0
        while not self.stopping:
            row = self._camera(camera_id)
            if row is None or not row["enabled"] or row["recording_mode"] != "continuous":
                self._set_status(camera_id, "stopped")
                return
            try:
                source = decrypt(row["stream_url_ciphertext"])
                safe_stream_url(source)
            except Exception:
                self._set_status(camera_id, "failed", "Camera credentials are unavailable")
                return
            output_dir = recordings_path() / camera_id / uuid.uuid4().hex
            output_dir.mkdir(parents=True, exist_ok=True)
            list_path = output_dir / "segments.csv"
            self._set_status(camera_id, "connecting")
            args = [
                executable, "-hide_banner", "-nostats", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-protocol_whitelist", _PROTOCOLS,
                "-rtsp_transport", "tcp", "-rw_timeout", "15000000", "-i", "pipe:0",
                "-map", "0:v:0", "-an", "-c:v", "copy", "-f", "segment",
                "-segment_time", str(SEGMENT_SECONDS), "-reset_timestamps", "1",
                "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
                "-segment_list", str(list_path), "-segment_list_type", "csv", "-strftime", "1",
                str(output_dir / "segment-%Y%m%dT%H%M%SZ.mp4"),
            ]
            env = os.environ.copy()
            env["TZ"] = "UTC"
            process: asyncio.subprocess.Process | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, env=env
                )
                if process.stdin is None:
                    raise OSError("Unable to pass stream source to FFmpeg")
                process.stdin.write(_manifest_bytes(source))
                await process.stdin.drain()
                process.stdin.close()
                while process.returncode is None and not self.stopping:
                    self._index_csv(camera_id, list_path, output_dir)
                    with connect() as db:
                        latest = db.execute(
                            "SELECT MAX(ends_at) AS latest FROM recording_segments WHERE camera_id=?",
                            (camera_id,),
                        ).fetchone()["latest"]
                    if latest:
                        recent_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=SEGMENT_SECONDS * 2)).isoformat()
                        if latest >= recent_cutoff:
                            self._set_status(camera_id, "recording")
                    await asyncio.sleep(2)
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                self._index_csv(camera_id, list_path, output_dir)
                if self.stopping:
                    return
                failures += 1
                self._set_status(camera_id, "reconnecting", "Camera stream interrupted; retrying")
            except asyncio.CancelledError:
                if process and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                self._index_csv(camera_id, list_path, output_dir)
                raise
            except OSError:
                failures += 1
                self._set_status(camera_id, "reconnecting", "Camera stream could not start; retrying")
            await asyncio.sleep(min(2 ** min(failures, 5), RECONNECT_MAX_SECONDS))
