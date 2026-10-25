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
from .motion import MotionClipAssembler
from .onvif_events import CameraMotionEvent, subscribe_and_pull
from .server_motion import FRAME_HEIGHT, FRAME_WIDTH, FrameMotionDetector
from .scheduling import effective_policy
from . import coverage, metrics, tamper, viewing_audit
from .health import recording_health
from .incidents import sync_incident, sync_storage_incidents

RECONNECT_MAX_SECONDS = 30
# An open coverage interval not confirmed for this long means the recorder was not running.
RESTART_GAP_AFTER = timedelta(seconds=15)


def segment_seconds() -> int:
    try:
        configured = int(os.environ.get("NVR_SEGMENT_SECONDS", "60"))
    except ValueError:
        configured = 60
    return max(10, min(configured, 3600))


def recordings_path() -> Path:
    return Path(os.environ.get("NVR_RECORDINGS_PATH", "/recordings")).resolve()


def motion_buffer_path() -> Path:
    return Path(os.environ.get("NVR_MOTION_BUFFER_PATH", "/tmp/nvr-motion-buffer")).resolve()


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


async def _stream_fault(source: str) -> str:
    """camera_offline when nothing answers at the camera's stream address, otherwise stream_error."""
    parts = urlsplit(source)
    if not parts.hostname:
        return "stream_error"
    try:
        port = parts.port or {"rtsp": 554, "rtsps": 322, "http": 80, "https": 443}.get(parts.scheme, 554)
        _, writer = await asyncio.wait_for(asyncio.open_connection(parts.hostname, port), timeout=3)
    except (OSError, ValueError, asyncio.TimeoutError):
        return "camera_offline"
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return "stream_error"


def _manifest_bytes(source: str) -> bytes:
    # Feed the source URL to FFmpeg over stdin so camera credentials never appear in argv.
    # RTSP options belong to the nested file opened by the concat demuxer.
    return f"ffconcat version 1.0\nfile {source}\noption rtsp_transport tcp\noption rw_timeout 15000000\n".encode("utf-8")


_PROTOCOLS = "concat,pipe,rtsp,rtsps,tcp,udp,rtp,tls,crypto"


def audio_recording_args(enabled: bool) -> list[str]:
    """Keep audio opt-in and normalize enabled audio to browser-friendly AAC."""
    return ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "96k"] if enabled else ["-an"]


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
        "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "json", "pipe:0",
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
        "audio": any(s.get("codec_type") == "audio" for s in streams),
        "video": {
            "codec": str(video.get("codec_name", "unknown")),
            "width": video.get("width"),
            "height": video.get("height"),
        },
    }


class RecordingManager:
    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.task_modes: dict[str, str] = {}
        self.scheduler_task: asyncio.Task[None] | None = None
        self.retention_task: asyncio.Task[None] | None = None
        self.tamper = tamper.TamperManager()
        self._dropped: set[str] = set()  # Segments already counted as dropped: the index is re-read every few seconds.
        self.storage_task: asyncio.Task[None] | None = None
        self.stopping = False

    async def start(self) -> None:
        self.stopping = False
        self._mark_restart_gaps()
        with connect() as db:
            viewing_audit.close_orphans(db)
        with connect() as db:
            rows = db.execute("SELECT id FROM cameras").fetchall()
        for row in rows:
            await self.sync_camera(row["id"])
        self.scheduler_task = asyncio.create_task(self._schedule_loop(), name="recording-schedule-supervisor")
        self.retention_task = asyncio.create_task(self._retention_loop(), name="recording-retention-cleanup")
        self.storage_task = asyncio.create_task(self._storage_loop(), name="recording-storage-health")
        await self.tamper.start()

    async def stop(self) -> None:
        self.stopping = True
        await self.tamper.stop()
        if self.scheduler_task and not self.scheduler_task.done():
            self.scheduler_task.cancel()
            await asyncio.gather(self.scheduler_task, return_exceptions=True)
        self.scheduler_task = None
        if self.retention_task and not self.retention_task.done():
            self.retention_task.cancel()
            await asyncio.gather(self.retention_task, return_exceptions=True)
        self.retention_task = None
        if self.storage_task and not self.storage_task.done():
            self.storage_task.cancel()
            await asyncio.gather(self.storage_task, return_exceptions=True)
        self.storage_task = None
        for task in list(self.tasks.values()):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.task_modes.clear()

    def ensure(self, camera_id: str) -> None:
        task = self.tasks.get(camera_id)
        if task is None or task.done():
            self.tasks[camera_id] = asyncio.create_task(self._run(camera_id, "continuous"), name=f"record-{camera_id}")
            self.task_modes[camera_id] = "continuous"

    async def remove(self, camera_id: str) -> None:
        task = self.tasks.pop(camera_id, None)
        self.task_modes.pop(camera_id, None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def sync_camera(self, camera_id: str, force_restart: bool = False) -> None:
        with connect() as db:
            policy = effective_policy(db, camera_id, datetime.now(timezone.utc))
            row = db.execute("SELECT enabled,recording_enabled,recording_status,archived_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
        desired = policy["mode"] if row and row["enabled"] and row["recording_enabled"] and not row["archived_at"] else "disabled"
        task = self.tasks.get(camera_id)
        if task is not None and self.task_modes.get(camera_id) == desired:
            if not task.done() or not force_restart:
                return
        await self.remove(camera_id)
        if desired in {"motion", "continuous"} and not self.stopping:
            self.task_modes[camera_id] = desired
            self.tasks[camera_id] = asyncio.create_task(self._run(camera_id, desired), name=f"record-{camera_id}")
        elif row and row["enabled"] and row["recording_enabled"] and policy["scheduled"]:
            if row["recording_status"] != "scheduled_idle":
                self._set_status(camera_id, "scheduled_idle")
        elif row and not row["archived_at"] and row["recording_status"] != "stopped":
            self._set_status(camera_id, "stopped")

    async def _schedule_loop(self) -> None:
        while not self.stopping:
            try:
                with connect() as db:
                    rows = db.execute("SELECT id FROM cameras").fetchall()
                await asyncio.gather(*(self.sync_camera(row["id"]) for row in rows))
                for row in rows:
                    self._sync_coverage(row["id"])
            except asyncio.CancelledError:
                raise
            except sqlite3.Error:
                pass
            await asyncio.sleep(5)

    async def _retention_loop(self) -> None:
        from .retention import cleanup_expired_recordings
        while not self.stopping:
            try:
                await asyncio.to_thread(cleanup_expired_recordings)
                await asyncio.to_thread(self._prune_viewing_audit)
            except asyncio.CancelledError:
                raise
            except (OSError, sqlite3.Error):
                pass
            await asyncio.sleep(300)

    @staticmethod
    def _prune_viewing_audit() -> None:
        with connect() as db:
            viewing_audit.prune(db)

    async def _storage_loop(self) -> None:
        while not self.stopping:
            try:
                await asyncio.to_thread(self._sync_storage_health)
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, sqlite3.Error):
                pass
            await asyncio.sleep(30)

    @staticmethod
    def _sync_storage_health() -> None:
        from .storage import storage_status
        status = storage_status()
        with connect() as db:
            sync_storage_incidents(db, status["volumes"])

    @staticmethod
    def _mark_restart_gaps(now: datetime | None = None) -> None:
        """While the recorder was not running nothing was recorded: close each open interval when it was last
        confirmed and record the time since as a worker_restart gap. An interval where no recording was expected stays open."""
        now = now or datetime.now(timezone.utc)
        with connect() as db:
            for interval in db.execute("SELECT * FROM coverage_intervals WHERE ends_at IS NULL").fetchall():
                checked = interval["checked_at"] or interval["starts_at"]
                if now - datetime.fromisoformat(checked.replace("Z", "+00:00")) < RESTART_GAP_AFTER:
                    continue
                if interval["coverage_kind"] == "expected_idle":
                    continue  # Nothing was expected: the interval carries on across the restart.
                db.execute("UPDATE coverage_intervals SET ends_at=? WHERE id=?", (checked, interval["id"]))
                db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,reason_code,starts_at,ends_at,checked_at) VALUES(?,?,?,?,?,?,?)",
                           (interval["camera_id"], "failure", "The recorder was not running", "worker_restart", checked, now.isoformat(), now.isoformat()))

    def _sync_coverage(self, camera_id: str) -> None:
        with connect() as db:
            row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
            if row is None:
                return
            policy = effective_policy(db, camera_id, datetime.now(timezone.utc))
            health = recording_health(
                enabled=bool(row["enabled"]), recording_enabled=bool(row["recording_enabled"]),
                mode=row["recording_mode"], effective_mode=str(policy["mode"]),
                recording_status=row["recording_status"], motion_source_status=row["motion_source_status"],
                last_video_at=row["last_video_at"], last_footage_write_at=row["last_footage_write_at"],
            )
            storage_faulty = db.execute(
                "SELECT 1 FROM incidents i, json_each(i.details_json, '$.affected_cameras') c "
                "WHERE i.fault_type='storage' AND i.fault_active=1 AND json_extract(c.value, '$.id')=? LIMIT 1", (camera_id,)).fetchone() is not None
            if health["video_fresh"] and row["stream_fault"]:
                db.execute("UPDATE cameras SET stream_fault=NULL WHERE id=?", (camera_id,))  # Fresh video: the stream works again.
                row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
            code = coverage.reason_code(row, policy, health, storage_faulty)
            active = db.execute("SELECT id,coverage_kind,reason_code FROM coverage_intervals WHERE camera_id=? AND ends_at IS NULL ORDER BY id DESC LIMIT 1", (camera_id,)).fetchone()
            kind = str(health["coverage_kind"])
            now = datetime.now(timezone.utc).isoformat()
            if active and active["coverage_kind"] == kind and active["reason_code"] == code:
                db.execute("UPDATE coverage_intervals SET reason=?,checked_at=? WHERE id=?", (str(health["detail"]), now, active["id"]))
            else:
                if active:
                    db.execute("UPDATE coverage_intervals SET ends_at=? WHERE id=?", (now, active["id"]))
                db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,reason_code,starts_at,checked_at) VALUES(?,?,?,?,?,?)",
                           (camera_id, kind, str(health["detail"]), code, now, now))
            for fault_type, coverage_kinds, title in (
                ("recording_failure", {"failure"}, "Recording coverage failure"),
                ("recording_uncertain", {"uncertain"}, "Recording coverage is uncertain"),
            ):
                sync_incident(
                    db,
                    incident_key=f"{fault_type}:{camera_id}",
                    camera_id=camera_id,
                    fault_type=fault_type,
                    summary=f"{title}: {row['name']} — {health['detail']}",
                    active=kind in coverage_kinds,
                    details={"coverage_kind": kind, "reason": str(health["detail"])},
                )

    def _desired_mode(self, camera_id: str) -> str:
        with connect() as db:
            return str(effective_policy(db, camera_id, datetime.now(timezone.utc))["mode"])

    def _camera(self, camera_id: str) -> sqlite3.Row | None:
        with connect() as db:
            return db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()

    def _set_status(self, camera_id: str, value: str, error: str | None = None, fault: str | None = None) -> None:
        """Record the recorder's state. ``fault`` says what went wrong when the state is a failure; it is kept while the recorder
        retries and cleared once the stream works (see ``_sync_coverage``) or recording stops."""
        with connect() as db:
            db.execute(
                "UPDATE cameras SET recording_status=?, last_error=?, "
                "stream_fault=CASE WHEN ? IN ('reconnecting','unavailable','failed') THEN ? WHEN ? IN ('recording','stopped','scheduled_idle') THEN NULL ELSE stream_fault END, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (value, error, value, fault, value, camera_id),
            )

    def _mark_video_fresh(self, camera_id: str) -> None:
        with connect() as db:
            db.execute("UPDATE cameras SET last_video_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (camera_id,))

    def _count_dropped(self, camera_id: str, name: str) -> None:
        if (key := f"{camera_id}/{name}") not in self._dropped:
            if len(self._dropped) >= 10_000:
                self._dropped.clear()
            self._dropped.add(key)
            metrics.segment_dropped(camera_id)

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
                # A file that is there but too small to be video was dropped; one that is gone was removed (retention), not lost.
                if root in media.parents and media.is_file():
                    self._count_dropped(camera_id, candidate.name)
                continue
            try:
                duration = max(0.0, float(row[2]) - float(row[1]))
                started = datetime.strptime(candidate.stem, "segment-%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except (ValueError, OverflowError):
                self._count_dropped(camera_id, candidate.name)
                continue
            relative = media.relative_to(root).as_posix()
            try:
                with connect() as db:
                    db.execute("BEGIN IMMEDIATE")  # The segment and its counters are one change.
                    try:
                        cur = db.execute(
                            "INSERT OR IGNORE INTO recording_segments(camera_id, relative_path, starts_at, ends_at, size_bytes) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (camera_id, relative, started.isoformat(), (started + timedelta(seconds=duration)).isoformat(), media.stat().st_size),
                        )
                        if cur.rowcount:
                            metrics.segment_written(db, camera_id, started + timedelta(seconds=duration))
                    except BaseException:
                        db.execute("ROLLBACK")
                        raise
                    db.execute("COMMIT")
                    indexed += cur.rowcount
            except sqlite3.Error:
                continue
        if indexed:
            with connect() as db:
                db.execute("UPDATE cameras SET last_footage_write_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),recording_fault_since=NULL WHERE id=?", (camera_id,))
        return indexed

    async def _run(self, camera_id: str, expected_mode: str) -> None:
        failures = 0
        while not self.stopping:
            row = self._camera(camera_id)
            if row is None or not row["enabled"] or not row["recording_enabled"] or self._desired_mode(camera_id) != expected_mode:
                self._set_status(camera_id, "stopped")
                return
            if expected_mode == "motion":
                should_retry = await self._run_motion(camera_id)
                row = self._camera(camera_id)
                if row is None or not row["enabled"] or not row["recording_enabled"] or self._desired_mode(camera_id) != expected_mode or self.stopping:
                    return
                if not should_retry:
                    return
                failures += 1
                await asyncio.sleep(min(2 ** min(failures, 5), RECONNECT_MAX_SECONDS))
            else:
                await self._run_continuous(camera_id, expected_mode)
                return

    async def _run_motion(self, camera_id: str) -> bool:
        row = self._camera(camera_id)
        if row is None or not row["enabled"] or not row["recording_enabled"]:
            self._set_status(camera_id, "stopped")
            return False
        fallback_enabled = bool(row["motion_fallback_enabled"])
        if not row["motion_service_url"] and not fallback_enabled:
            with connect() as db:
                db.execute("UPDATE cameras SET recording_status='unsupported',motion_source_status='unsupported',server_motion_status='disabled',last_error='Configure an ONVIF event service or enable server motion fallback' WHERE id=?", (camera_id,))
            return False
        executable = shutil.which("ffmpeg")
        if not executable:
            with connect() as db:
                db.execute("UPDATE cameras SET recording_status='unavailable',motion_source_status='unavailable',server_motion_status=?,last_error='FFmpeg is not installed' WHERE id=?", ("unavailable" if fallback_enabled else "disabled", camera_id))
            return False
        try:
            source = decrypt(row["stream_url_ciphertext"])
            safe_stream_url(source)
            credentials = json.loads(decrypt(row["motion_credentials_ciphertext"])) if row["motion_credentials_ciphertext"] else {"username": "", "password": ""}
        except Exception:
            with connect() as db:
                db.execute("UPDATE cameras SET recording_status='failed',motion_source_status='configuration_error',last_error='Camera credentials are unavailable' WHERE id=?", (camera_id,))
            return False

        spool = motion_buffer_path() / camera_id
        spool.mkdir(parents=True, exist_ok=True)
        assembler = MotionClipAssembler(camera_id, spool, row["motion_pre_roll_seconds"], row["motion_post_roll_seconds"])
        self._set_status(camera_id, "buffering")
        with connect() as db:
            db.execute("UPDATE cameras SET motion_source_status=?,server_motion_status=?,last_error=NULL WHERE id=?",
                       ("connecting" if row["motion_service_url"] else "unsupported", "waiting" if fallback_enabled else "disabled", camera_id))
        event_task: asyncio.Task[None] | None = None
        fallback_task: asyncio.Task[None] | None = None
        proc: asyncio.subprocess.Process | None = None
        source_state = {"value": "connecting" if row["motion_service_url"] else "unsupported"}
        source_changed = asyncio.Event()
        try:
            list_size = min(1024, max(40, int((row["motion_pre_roll_seconds"] + row["motion_post_roll_seconds"] + 60) / 2)))
            audio_args = audio_recording_args(bool(row["audio_enabled"]))
            args = [
                executable, "-hide_banner", "-nostats", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-protocol_whitelist", _PROTOCOLS,
                "-i", "pipe:0", "-map", "0:v:0", *audio_args, "-c:v", "copy",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", str(list_size),
                "-hls_flags", "delete_segments+append_list+omit_endlist+program_date_time",
                "-hls_segment_filename", str(spool / "segment-%06d.ts"), str(spool / "live.m3u8"),
            ]
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            if proc.stdin is None:
                raise OSError("Unable to pass stream source to FFmpeg")
            proc.stdin.write(_manifest_bytes(source))
            await proc.stdin.drain()
            proc.stdin.close()

            def source_status(value: str) -> None:
                source_state["value"] = value
                source_changed.set()
                healthy = value == "healthy"
                status = "buffering" if healthy else ("reconnecting" if value == "unavailable" else "degraded")
                error = None if healthy else ("ONVIF event subscription is unavailable" if value == "unavailable" else "ONVIF event service is not usable")
                if not healthy and fallback_enabled:
                    error = "ONVIF events are degraded; server motion fallback is starting"
                with connect() as db:
                    db.execute("UPDATE cameras SET recording_status=?,motion_source_status=?,server_motion_status=?,last_error=? WHERE id=?",
                               (status, value, "waiting" if healthy and fallback_enabled else ("disabled" if not fallback_enabled else "detecting"), error, camera_id))

            async def on_event(event) -> None:
                await assembler.ingest(event)

            if row["motion_service_url"]:
                event_task = asyncio.create_task(
                    subscribe_and_pull(row["motion_service_url"], credentials.get("username", ""), credentials.get("password", ""), on_event, source_status),
                    name=f"onvif-events-{camera_id}",
                )
            else:
                source_status("unsupported")
            if fallback_enabled:
                fallback_task = asyncio.create_task(
                    self._watch_server_motion_fallback(
                        camera_id, source, json.loads(row["motion_zones_json"]), row["motion_sensitivity"],
                        source_state, source_changed, assembler,
                    ),
                    name=f"server-motion-{camera_id}",
                )
            while not self.stopping:
                await assembler.poll()
                newest = max(spool.glob("segment-*.ts"), key=lambda path: path.stat().st_mtime, default=None)
                if newest is not None:
                    try:
                        if datetime.now().timestamp() - newest.stat().st_mtime <= 10:
                            self._mark_video_fresh(camera_id)
                    except OSError:
                        pass
                if proc.returncode is not None:
                    self._set_status(camera_id, "reconnecting", "Camera stream buffer interrupted; retrying", fault=await _stream_fault(source))
                    break
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError):
            with connect() as db:
                db.execute("UPDATE cameras SET recording_status='reconnecting',motion_source_status='unavailable',last_error='Camera motion buffer could not start',stream_fault=? WHERE id=?",
                           (await _stream_fault(source), camera_id))
        finally:
            if event_task:
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
            if fallback_task:
                fallback_task.cancel()
                await asyncio.gather(fallback_task, return_exceptions=True)
            await assembler.stop()
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            if self.stopping:
                self._set_status(camera_id, "stopped")
            shutil.rmtree(spool, ignore_errors=True)
        return True

    async def _watch_server_motion_fallback(
        self,
        camera_id: str,
        source: str,
        zones: list[dict[str, float]],
        sensitivity: int,
        source_state: dict[str, str],
        source_changed: asyncio.Event,
        assembler: MotionClipAssembler,
    ) -> None:
        while not self.stopping:
            if source_state["value"] in {"healthy", "connecting"}:
                with connect() as db:
                    db.execute("UPDATE cameras SET server_motion_status='waiting' WHERE id=?", (camera_id,))
                source_changed.clear()
                if source_state["value"] in {"healthy", "connecting"}:
                    await source_changed.wait()
                continue
            try:
                await self._detect_server_motion(camera_id, source, zones, sensitivity, source_state, assembler)
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, asyncio.IncompleteReadError, TimeoutError):
                if source_state["value"] in {"healthy", "connecting"}:
                    with connect() as db:
                        db.execute("UPDATE cameras SET server_motion_status='waiting' WHERE id=?", (camera_id,))
                else:
                    with connect() as db:
                        db.execute("UPDATE cameras SET server_motion_status='unavailable',recording_status='unavailable',last_error='ONVIF events and server motion fallback are unavailable' WHERE id=?", (camera_id,))
            if source_state["value"] not in {"healthy", "connecting"} and not self.stopping:
                await asyncio.sleep(2)

    async def _detect_server_motion(
        self,
        camera_id: str,
        source: str,
        zones: list[dict[str, float]],
        sensitivity: int,
        source_state: dict[str, str],
        assembler: MotionClipAssembler,
    ) -> None:
        executable = shutil.which("ffmpeg")
        if not executable:
            raise OSError("FFmpeg is not installed")
        detector = FrameMotionDetector(zones, sensitivity)
        args = [
            executable, "-hide_banner", "-nostats", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-protocol_whitelist", _PROTOCOLS,
            "-i", "pipe:0", "-map", "0:v:0", "-vf", f"fps=2,scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=area,format=gray",
            "-an", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1",
        ]
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            if process.stdin is None or process.stdout is None:
                raise OSError("Unable to start server motion detector")
            process.stdin.write(_manifest_bytes(source))
            await process.stdin.drain()
            process.stdin.close()
            with connect() as db:
                db.execute("UPDATE cameras SET server_motion_status='detecting',recording_status='buffering',last_error='ONVIF events are degraded; server motion fallback is active' WHERE id=?", (camera_id,))
            frame_size = FRAME_WIDTH * FRAME_HEIGHT
            while not self.stopping and source_state["value"] not in {"healthy", "connecting"}:
                frame = await asyncio.wait_for(process.stdout.readexactly(frame_size), timeout=5)
                transition = detector.process(frame)
                if transition is not None:
                    await assembler.ingest(CameraMotionEvent(
                        transition, datetime.now(timezone.utc), "server-motion-difference", source="server",
                    ))
                if process.returncode is not None:
                    raise OSError("Server motion detector stopped")
        finally:
            if detector.active:
                await assembler.ingest(CameraMotionEvent(
                    False, datetime.now(timezone.utc), "server-motion-difference", source="server",
                ))
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            if source_state["value"] in {"healthy", "connecting"}:
                with connect() as db:
                    db.execute("UPDATE cameras SET server_motion_status='waiting' WHERE id=?", (camera_id,))

    async def _run_continuous(self, camera_id: str, expected_mode: str = "continuous") -> None:
        executable = shutil.which("ffmpeg")
        if not executable:
            self._set_status(camera_id, "unavailable", "FFmpeg is not installed")
            return
        segment_duration = segment_seconds()
        failures = 0
        while not self.stopping:
            row = self._camera(camera_id)
            if row is None or not row["enabled"] or not row["recording_enabled"] or self._desired_mode(camera_id) != expected_mode:
                self._set_status(camera_id, "stopped")
                return
            try:
                source = decrypt(row["stream_url_ciphertext"])
                safe_stream_url(source)
            except Exception:
                self._set_status(camera_id, "failed", "Camera credentials are unavailable", fault="stream_error")
                return
            try:
                from .storage import camera_recordings_dir
                output_dir = camera_recordings_dir(camera_id) / uuid.uuid4().hex
                output_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self._set_status(camera_id, "unavailable", "Assigned recording volume is missing or not writable", fault="disk_unavailable")
                await asyncio.sleep(5)
                continue
            list_path = output_dir / "segments.csv"
            self._set_status(camera_id, "connecting")
            audio_args = audio_recording_args(bool(row["audio_enabled"]))
            args = [
                executable, "-hide_banner", "-nostats", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-protocol_whitelist", _PROTOCOLS,
                "-i", "pipe:0",
                "-map", "0:v:0", *audio_args, "-c:v", "copy", "-f", "segment",
                "-segment_time", str(segment_duration), "-reset_timestamps", "1",
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
                    newest = max(output_dir.glob("segment-*.mp4"), key=lambda path: path.stat().st_mtime, default=None)
                    if newest is not None:
                        try:
                            if datetime.now().timestamp() - newest.stat().st_mtime <= 10:
                                self._mark_video_fresh(camera_id)
                        except OSError:
                            pass
                    with connect() as db:
                        latest = db.execute(
                            "SELECT MAX(ends_at) AS latest FROM recording_segments WHERE camera_id=?",
                            (camera_id,),
                        ).fetchone()["latest"]
                    if latest:
                        recent_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=segment_duration * 2)).isoformat()
                        if latest >= recent_cutoff:
                            self._set_status(camera_id, "recording")
                        if latest and latest < recent_cutoff:
                            db.execute("UPDATE cameras SET recording_fault_since=COALESCE(recording_fault_since,strftime('%Y-%m-%dT%H:%M:%fZ','now')) WHERE id=?", (camera_id,))
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
                self._set_status(camera_id, "reconnecting", "Camera stream interrupted; retrying", fault=await _stream_fault(source))
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
                self._set_status(camera_id, "reconnecting", "Camera stream could not start; retrying", fault=await _stream_fault(source))
            await asyncio.sleep(min(2 ** min(failures, 5), RECONNECT_MAX_SECONDS))
