from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import sqlite3
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .recording import recordings_path
from .retention import acquire_playback, release_playback
from .storage import recording_file_path
from .store import connect

MAX_EXPORT_WORKERS = 1
MAX_ACTIVE_EXPORTS = 4
MAX_EXPORT_CAMERAS = 8
MAX_EXPORT_WINDOW = timedelta(hours=24)
MAX_EXPORT_SOURCE_BYTES = 100 * 1024**3
MAX_EXPORT_SEGMENTS = 5000
EXPORT_DIRECTORY = "exports"
MAX_VERIFY_PACKAGE_BYTES = 2 * 1024**3


def verify_evidence_bytes(package: bytes, expected_export_id: str | None = None) -> dict[str, Any]:
    """Verify the evidence manifest against the exact uploaded ZIP member bytes.

    This deliberately does not extract files or validate that media is playable.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or any(name.startswith("/") or ".." in Path(name).parts for name in names):
                raise ValueError("invalid archive paths")
            if sum(item.file_size for item in infos) > MAX_VERIFY_PACKAGE_BYTES:
                raise ValueError("uncompressed package exceeds the verification size limit")
            if names.count("integrity.json") != 1:
                raise ValueError("integrity manifest is missing or duplicated")
            manifest = json.loads(archive.read("integrity.json"))
            if manifest.get("algorithm") != "sha256" or not isinstance(manifest.get("files"), list):
                raise ValueError("unsupported integrity manifest")
            expected = {entry["path"]: entry for entry in manifest["files"]}
            if len(expected) != len(manifest["files"]):
                raise ValueError("integrity manifest contains duplicate paths")
            delivered = set(names) - {"integrity.json"}
            if delivered != set(expected):
                return {"checksums_valid": False, "files": [], "error": "package contents do not match the integrity manifest"}
            if expected_export_id is not None:
                metadata = json.loads(archive.read("metadata.json"))
                if metadata.get("export_id") != expected_export_id or metadata.get("audit_reference") != {"resource_type": "export", "resource_id": expected_export_id}:
                    return {"checksums_valid": False, "files": [], "error": "package identity does not match the requested export"}
            files = []
            valid = True
            for name in sorted(delivered):
                content = archive.read(name)
                digest = hashlib.sha256(content).hexdigest()
                entry = expected[name]
                matches = len(content) == entry.get("size_bytes") and digest == entry.get("sha256")
                valid = valid and matches
                files.append({"path": name, "size_bytes": len(content), "sha256": digest, "matches": matches})
            return {"checksums_valid": valid, "files": files, "error": None if valid else "one or more file checksums do not match"}
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"checksums_valid": False, "files": [], "error": f"invalid evidence package: {str(exc)[:200]}"}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ExportCancelled(Exception):
    pass


def recover_interrupted_exports() -> None:
    """Mark jobs lost with the prior process as failures; queued jobs survive restart."""
    with connect() as db:
        rows = db.execute("SELECT id,requested_by FROM exports WHERE status='running'").fetchall()
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            db.execute(
                "UPDATE exports SET status='failed',error=?,updated_at=?,completed_at=? WHERE id=? AND status='running'",
                ("Export interrupted by application restart", now, now, row["id"]),
            )
            db.execute(
                "INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                (row["requested_by"], "export_failed", "export", row["id"], json.dumps({"reason": "application_restart"})),
            )
            output_root = recordings_path() / EXPORT_DIRECTORY
            (output_root / f"{row['id']}.zip").unlink(missing_ok=True)
            (output_root / f"{row['id']}.zip.partial").unlink(missing_ok=True)
            shutil.rmtree(output_root / f".{row['id']}.work", ignore_errors=True)


def _interval_metadata(start: datetime, end: datetime, segments: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    spans = []
    for segment in segments:
        actual_start = max(start, _parse_time(segment["starts_at"]))
        actual_end = min(end, _parse_time(segment["ends_at"]))
        if actual_end > actual_start:
            spans.append((actual_start, actual_end))
    spans.sort()
    merged: list[tuple[datetime, datetime]] = []
    for span_start, span_end in spans:
        if merged and span_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span_end))
        else:
            merged.append((span_start, span_end))
    actual = [{"start": s.isoformat(), "end": e.isoformat()} for s, e in merged]
    missing = []
    cursor = start
    for span_start, span_end in merged:
        if span_start > cursor:
            missing.append({"start": cursor.isoformat(), "end": span_start.isoformat()})
        cursor = max(cursor, span_end)
    if cursor < end:
        missing.append({"start": cursor.isoformat(), "end": end.isoformat()})
    return actual, missing


def _parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _ffconcat_quote(path: Path) -> str:
    # ffconcat uses a small quoting language, not shell quoting. Newlines are
    # excluded because segment paths are validated relative paths in storage.
    rendered = path.as_posix()
    if "\n" in rendered or "\r" in rendered:
        raise ValueError("Recording path contains an invalid line break")
    return "file '" + rendered.replace("'", "'\\''") + "'\n"


def _job_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "start": row["requested_start"],
        "end": row["requested_end"],
        "camera_ids": json.loads(row["camera_ids_json"]),
        "output_modes": json.loads(row["output_modes_json"]),
        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
        "error": row["error"],
        "download_url": f"/api/exports/{row['id']}/download" if row["status"] == "completed" and row["output_relative_path"] else None,
        "cancel_requested": bool(row["cancel_requested"]),
    }


class ExportManager:
    """Persistent, single-worker export queue with bounded queued work."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()
        self._stopping = False

    async def start(self) -> None:
        recover_interrupted_exports()
        with connect() as db:
            pending = [row[0] for row in db.execute("SELECT id FROM exports WHERE status='queued' ORDER BY created_at")]
        self._stopping = False
        self._worker = asyncio.create_task(self._run(), name="nvr-export-worker")
        for job_id in pending:
            self._queue.put_nowait(job_id)

    async def stop(self) -> None:
        self._stopping = True
        for process in list(self._processes.values()):
            if process.returncode is None:
                process.terminate()
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        with connect() as db:
            active = db.execute("SELECT id,requested_by FROM exports WHERE status='running'").fetchall()
            now = datetime.now(timezone.utc).isoformat()
            for row in active:
                db.execute("UPDATE exports SET status='failed',error=?,updated_at=?,completed_at=? WHERE id=?", ("Export interrupted by application shutdown", now, now, row["id"]))
                db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)", (row["requested_by"], "export_failed", "export", row["id"], '{"reason":"application_shutdown"}'))

    def enqueue(self, job_id: str) -> None:
        if self._worker is None or self._worker.done():
            raise RuntimeError("Export worker is not running")
        self._queue.put_nowait(job_id)

    async def cancel(self, job_id: str) -> None:
        self._cancelled.add(job_id)
        process = self._processes.get(job_id)
        if process and process.returncode is None:
            process.terminate()

    async def _run(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if self._stopping:
                    return
                with connect() as db:
                    row = db.execute("SELECT status FROM exports WHERE id=?", (job_id,)).fetchone()
                if not row or row["status"] != "queued":
                    continue
                await self._execute(job_id)
            finally:
                self._queue.task_done()

    async def _execute(self, job_id: str) -> None:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM exports WHERE id=? AND status='queued'", (job_id,)).fetchone()
            if row is None:
                return
            claimed = db.execute("UPDATE exports SET status='running',updated_at=? WHERE id=? AND status='queued'", (datetime.now(timezone.utc).isoformat(), job_id))
            if claimed.rowcount != 1:
                return
            camera_ids = json.loads(row["camera_ids_json"])
            requester = row["requested_by"]
            start, end = _parse_time(row["requested_start"]), _parse_time(row["requested_end"])

        acquired: list[Path] = []
        work_dir = recordings_path() / EXPORT_DIRECTORY / f".{job_id}.work"
        final_path = recordings_path() / EXPORT_DIRECTORY / f"{job_id}.zip"
        partial_path = final_path.with_suffix(".zip.partial")
        try:
            if job_id in self._cancelled:
                raise ExportCancelled()
            work_dir.mkdir(parents=True, exist_ok=False)
            with connect() as db:
                # Retention also takes this write lock before unlinking a segment.
                # Keep it until the in-memory lease is installed for every source.
                db.execute("BEGIN IMMEDIATE")
                per_camera: list[dict[str, Any]] = []
                total_segments = 0
                total_source_bytes = 0
                for index, camera_id in enumerate(camera_ids):
                    camera = db.execute("SELECT id,name FROM cameras WHERE id=?", (camera_id,)).fetchone()
                    if camera is None:
                        raise RuntimeError("A selected camera was removed before export started")
                    segments = [dict(item) for item in db.execute(
                        "SELECT id,relative_path,starts_at,ends_at,size_bytes FROM recording_segments "
                        "WHERE camera_id=? AND starts_at<? AND ends_at>? ORDER BY starts_at",
                        (camera_id, end.isoformat(), start.isoformat()),
                    ).fetchall()]
                    per_camera.append({"camera_id": camera_id, "name": camera["name"], "index": index, "segments": segments})
                    total_segments += len(segments)
                    total_source_bytes += sum(int(segment["size_bytes"]) for segment in segments)
                if not total_segments:
                    raise RuntimeError("No indexed footage is available in the requested interval")
                if total_segments > MAX_EXPORT_SEGMENTS or total_source_bytes > MAX_EXPORT_SOURCE_BYTES:
                    raise RuntimeError("Export source exceeds the supported size or segment limit")
                for camera in per_camera:
                    for segment in camera["segments"]:
                        path = recording_file_path(segment["relative_path"])
                        if not path.is_file():
                            raise RuntimeError("A source recording is unavailable; no partial export was produced")
                        acquire_playback(path)
                        acquired.append(path)

            metadata: dict[str, Any] = {
                "schema_version": 1,
                "package_type": "nvr_evidence",
                "export_id": job_id,
                "audit_reference": {"resource_type": "export", "resource_id": job_id},
                "requested_by_user_id": requester,
                "requested_interval": {"start": start.isoformat(), "end": end.isoformat()},
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "timestamp_provenance": "All timestamps are ISO 8601 with explicit UTC offsets and normalized to UTC. Recording intervals come from the NVR segment index; camera clock accuracy and timestamp authenticity are not independently attested.",
                "cameras": [],
                "notes": ["Video is exported without audio.", "Missing intervals are listed explicitly; the video concatenates available clips and therefore does not encode wall-clock gaps.", "SHA-256 detects changes relative to this manifest but is not a digital signature and does not establish who created the package or when."],
            }
            for camera in per_camera:
                if job_id in self._cancelled:
                    raise ExportCancelled()
                camera_segments = camera["segments"]
                actual, missing = _interval_metadata(start, end, camera_segments)
                entry: dict[str, Any] = {
                    "camera_id": camera["camera_id"], "camera_name": camera["name"],
                    "actual_intervals": actual, "missing_intervals": missing,
                    "segment_count": len(camera_segments), "video_file": None,
                }
                if camera_segments:
                    manifest = work_dir / f"camera-{camera['index']}.ffconcat"
                    clip_path = work_dir / f"camera-{camera['index']}.mp4"
                    lines = ["ffconcat version 1.0\n"]
                    for segment in camera_segments:
                        segment_start = _parse_time(segment["starts_at"])
                        segment_end = _parse_time(segment["ends_at"])
                        clip_start, clip_end = max(start, segment_start), min(end, segment_end)
                        lines.append(_ffconcat_quote(recording_file_path(segment["relative_path"])))
                        lines.append(f"inpoint {(clip_start - segment_start).total_seconds():.6f}\n")
                        lines.append(f"outpoint {(clip_end - segment_start).total_seconds():.6f}\n")
                    manifest.write_text("".join(lines), encoding="utf-8")
                    await self._run_ffmpeg(job_id, [
                        "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(manifest),
                        "-map", "0:v:0", "-an", "-vf", "fps=15,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1",
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-movflags", "+faststart", "-y", str(clip_path),
                    ])
                    if job_id in self._cancelled:
                        raise ExportCancelled()
                    if not clip_path.is_file() or clip_path.stat().st_size == 0:
                        raise RuntimeError(f"No playable video was produced for camera {camera['name']}")
                    entry["video_file"] = f"camera-{camera['index']}.mp4"
                metadata["cameras"].append(entry)

            work_dir.mkdir(parents=True, exist_ok=True)
            metadata_path = work_dir / "metadata.json"
            metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
            package_files = [metadata_path] + [work_dir / item["video_file"] for item in metadata["cameras"] if item["video_file"]]
            integrity = {
                "schema_version": 1,
                "algorithm": "sha256",
                "files": [
                    {"path": path.name, "size_bytes": path.stat().st_size, "sha256": _file_sha256(path)}
                    for path in package_files
                ],
            }
            integrity_path = work_dir / "integrity.json"
            integrity_path.write_text(json.dumps(integrity, indent=2, sort_keys=True), encoding="utf-8")
            if job_id in self._cancelled:
                raise ExportCancelled()
            final_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(partial_path, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.write(metadata_path, "metadata.json")
                archive.write(integrity_path, "integrity.json")
                for item in metadata["cameras"]:
                    if item["video_file"]:
                        archive.write(work_dir / item["video_file"], item["video_file"])
            if job_id in self._cancelled:
                raise ExportCancelled()
            os.replace(partial_path, final_path)
            with connect() as db:
                updated = db.execute("UPDATE exports SET status='completed',metadata_json=?,output_relative_path=?,updated_at=?,completed_at=?,error=NULL WHERE id=? AND status='running' AND cancel_requested=0",
                           (json.dumps(metadata, separators=(",", ":")), f"{EXPORT_DIRECTORY}/{job_id}.zip", datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), job_id))
                if updated.rowcount != 1:
                    raise ExportCancelled()
                db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                           (requester, "export_completed", "export", job_id, json.dumps({"camera_count": len(camera_ids), "segment_count": total_segments})))
        except ExportCancelled:
            partial_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            with connect() as db:
                db.execute("UPDATE exports SET status='cancelled',cancel_requested=0,error=NULL,updated_at=?,completed_at=? WHERE id=? AND status='running'", (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), job_id))
                db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)", (requester, "export_cancelled", "export", job_id, "{}"))
        except asyncio.CancelledError:
            partial_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            partial_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            with connect() as db:
                db.execute("UPDATE exports SET status='failed',error=?,updated_at=?,completed_at=? WHERE id=? AND status='running'", (str(exc)[:500], datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), job_id))
                db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)", (requester, "export_failed", "export", job_id, json.dumps({"reason": str(exc)[:500]})))
        finally:
            self._processes.pop(job_id, None)
            self._cancelled.discard(job_id)
            for path in acquired:
                release_playback(path)
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _run_ffmpeg(self, job_id: str, args: list[str]) -> None:
        executable = shutil.which("ffmpeg")
        if not executable:
            raise RuntimeError("FFmpeg is unavailable; export cannot run")
        process = await asyncio.create_subprocess_exec(
            executable, *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        self._processes[job_id] = process
        # Cancellation can arrive while create_subprocess_exec is yielding,
        # before this process is visible to ExportManager.cancel(). Recheck
        # immediately after registration so that job cannot run uncancellable.
        if job_id in self._cancelled and process.returncode is None:
            process.terminate()
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=60 * 60)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RuntimeError("Export encoding exceeded the one-hour job limit") from exc
        except asyncio.CancelledError:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            raise
        finally:
            self._processes.pop(job_id, None)
        if job_id in self._cancelled:
            raise ExportCancelled()
        if process.returncode != 0:
            reason = stderr.decode("utf-8", errors="replace")[-400:].strip()
            raise RuntimeError(reason or "FFmpeg could not create a playable export")


def create_export_record(
    requested_by: int, camera_ids: list[str], start: datetime, end: datetime,
    output_modes: list[str] | None = None,
) -> str:
    output_modes = output_modes or ["individual", "grid"]
    if not output_modes or len(set(output_modes)) != len(output_modes) or any(mode not in {"individual", "grid"} for mode in output_modes):
        raise ValueError("Choose individual, grid, or both export outputs")
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute("SELECT COUNT(*) FROM exports WHERE status IN ('queued','running')").fetchone()[0]
        if active >= MAX_ACTIVE_EXPORTS:
            raise OverflowError("The export queue is full; wait for an existing job to finish")
        placeholders = ",".join("?" for _ in camera_ids)
        present = {row[0] for row in db.execute(f"SELECT id FROM cameras WHERE id IN ({placeholders})", tuple(camera_ids))}
        if present != set(camera_ids):
            raise LookupError("Export request contains an unknown camera")
        segments = db.execute(
            f"SELECT id,size_bytes FROM recording_segments WHERE camera_id IN ({placeholders}) AND starts_at<? AND ends_at>?",
            (*camera_ids, end.isoformat(), start.isoformat()),
        ).fetchall()
        count = len(segments)
        size = sum(int(segment["size_bytes"]) for segment in segments)
        if not count:
            raise LookupError("No indexed footage is available in the requested interval")
        if count > MAX_EXPORT_SEGMENTS or size > MAX_EXPORT_SOURCE_BYTES:
            raise OverflowError("Export source exceeds the supported size or segment limit")
        db.execute("INSERT INTO exports(id,requested_by,camera_ids_json,output_modes_json,requested_start,requested_end,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'queued',?,?)",
                   (job_id, requested_by, json.dumps(camera_ids), json.dumps(output_modes), start.isoformat(), end.isoformat(), now, now))
        db.executemany("INSERT INTO export_sources(export_id,segment_id) VALUES(?,?)", ((job_id, segment["id"]) for segment in segments))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (requested_by, "export_requested", "export", job_id, json.dumps({"cameras": len(camera_ids), "start": start.isoformat(), "end": end.isoformat(), "output_modes": output_modes})))
    return job_id


def get_export(job_id: str, requester_id: int, is_admin: bool = False) -> sqlite3.Row | None:
    with connect() as db:
        if is_admin:
            return db.execute("SELECT * FROM exports WHERE id=?", (job_id,)).fetchone()
        return db.execute("SELECT * FROM exports WHERE id=? AND requested_by=?", (job_id, requester_id)).fetchone()


def list_exports(requester_id: int, is_admin: bool = False) -> list[dict[str, Any]]:
    with connect() as db:
        if is_admin:
            rows = db.execute("SELECT * FROM exports ORDER BY created_at DESC LIMIT 50").fetchall()
        else:
            rows = db.execute("SELECT * FROM exports WHERE requested_by=? ORDER BY created_at DESC LIMIT 50", (requester_id,)).fetchall()
    return [_job_view(row) for row in rows]


def request_cancel(job_id: str, actor_id: int, is_admin: bool = False) -> str:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if is_admin:
            row = db.execute("SELECT * FROM exports WHERE id=?", (job_id,)).fetchone()
        else:
            row = db.execute("SELECT * FROM exports WHERE id=? AND requested_by=?", (job_id, actor_id)).fetchone()
        if row is None:
            raise LookupError("Export job not found")
        if row["status"] == "queued":
            changed = db.execute("UPDATE exports SET status='cancelled',updated_at=?,completed_at=? WHERE id=? AND status='queued'", (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), job_id))
            if changed.rowcount != 1:
                latest = db.execute("SELECT status FROM exports WHERE id=?", (job_id,)).fetchone()
                if latest and latest["status"] == "running":
                    db.execute("UPDATE exports SET cancel_requested=1,updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), job_id))
                    db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)", (actor_id, "export_cancel_requested", "export", job_id, "{}"))
                    return "cancel_requested"
                return str(latest["status"]) if latest else "missing"
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)", (actor_id, "export_cancelled", "export", job_id, "{}"))
            return "cancelled"
        if row["status"] == "running":
            db.execute("UPDATE exports SET cancel_requested=1,updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), job_id))
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)", (actor_id, "export_cancel_requested", "export", job_id, "{}"))
            return "cancel_requested"
        return str(row["status"])


def export_output_path(row: sqlite3.Row) -> Path:
    if row["status"] != "completed" or not row["output_relative_path"]:
        raise FileNotFoundError("Export download is not ready")
    root = recordings_path().resolve()
    path = (root / row["output_relative_path"]).resolve()
    if root not in path.parents:
        raise ValueError("Export path is invalid")
    return path
