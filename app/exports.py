from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import IO, Any, Callable

from . import acceleration, copies_budget, footage_chain, privacy, redaction, viewing_audit
from . import dewarp as dewarp_geometry
from .display_settings import format_display_datetime
from .processes import end_process
from .recording import recordings_path
from .retention import acquire_playback, release_playback, unlink_if_unleased
from .storage import fetch_cold_sources, recording_file_path
from .store import connect
from app.clock import parse as clock_parse

POLL_SECONDS = 2
# A job marked running that nothing here is rendering is failed once it has shown no sign of life (a claim, a progress step) for this long.
ORPHAN_AFTER_SECONDS = 60


def _silent_for(updated_at: str | None, now: datetime, seconds: float) -> bool:
    try:
        return updated_at is None or (now - clock_parse(updated_at)).total_seconds() >= seconds
    except (TypeError, ValueError):
        return True
MAX_ACTIVE_EXPORTS = 4
MAX_EXPORT_CAMERAS = 16
MAX_EXPORT_WINDOW = timedelta(hours=24)
MAX_EXPORT_SOURCE_BYTES = 100 * 1024**3
MAX_EXPORT_SEGMENTS = 5000
EXPORT_DIRECTORY = "exports"
MAX_VERIFY_PACKAGE_BYTES = 2 * 1024**3
MAX_EXPORT_PACKAGE_BYTES = 2 * 1024**3
MIN_EXPORT_FREE_BYTES = 256 * 1024**2
EXPORT_RETENTION = timedelta(days=7)  # The default; an administrator may choose 1 to 30 days (export_settings, #362)
MAX_EXPORT_RETENTION_DAYS = 30


def retention_days(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT retention_days FROM export_settings WHERE singleton_id=1").fetchone()
    return int(row[0]) if row else EXPORT_RETENTION.days


OPEN_FILE_RESERVE = 256  # Files a process needs for everything else while an export holds its sources open


def max_open_sources() -> int:
    """How many recorded files an export may hold open at once in this process (each source stays reserved, with a file open, for the
    whole job). The process's own limit is raised to the most the system allows first: a common default of 1,024 failed an export of 1,100
    files with "Too many open files", while requests of up to 5,000 were accepted (#356)."""
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = hard if hard != resource.RLIM_INFINITY else max(soft, 1 << 20)
    if soft != resource.RLIM_INFINITY and soft < wanted:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
            soft = wanted
        except (ValueError, OSError):
            pass
    limit = MAX_EXPORT_SEGMENTS + OPEN_FILE_RESERVE if soft == resource.RLIM_INFINITY else soft
    return max(0, limit - OPEN_FILE_RESERVE)


def _too_many_files(count: int) -> str | None:
    allowed = max_open_sources()
    if count <= allowed:
        return None
    return (f"This export needs {count} recorded files open at once, more than this system allows ({allowed}). "
            "Choose a shorter time or fewer cameras, or raise the container's open-file limit (nofile).")


def free_floor(directory: Path) -> int:
    """How much free space an export or case package must leave on the recordings volume: its recording headroom, below which the recorder
    deletes the oldest footage, and never less than ``MIN_EXPORT_FREE_BYTES`` (#362)."""
    from . import headroom

    try:
        capacity = shutil.disk_usage(directory).total
    except OSError:
        return MIN_EXPORT_FREE_BYTES
    with connect() as db:
        return max(MIN_EXPORT_FREE_BYTES, headroom.effective_headroom(db, "default", capacity))


def verify_chain_member(archive: zipfile.ZipFile, installation_key: str | None = None) -> dict[str, Any]:
    """Check the package's signed chain extract offline, with the public key it carries.

    With ``installation_key``, a chain signed by any other key is invalid: a package can carry a key of its own making."""
    try:
        package = json.loads(archive.read("chain.json"))
        cameras = []
        for camera in package["cameras"]:
            if not camera.get("chain"):
                cameras.append({"camera_id": camera.get("camera_id"), "status": "not_signed", "error": None})
                continue
            result = footage_chain.verify_extract(camera["chain"], camera.get("sources", []))
            entry = {"camera_id": camera.get("camera_id"), "status": "valid" if result["valid"] else "invalid", "error": result["error"],
                     "public_key": result["public_key"], "unsigned_segments": result["unsigned"]}
            # A chain is signed for one camera. Another camera's valid chain placed under this camera's name proves nothing about it (#363).
            owners = {camera["chain"].get("camera_id")} | {link.get("camera_id") for link in camera["chain"].get("links") or []}
            if entry["status"] == "valid" and owners != {camera.get("camera_id")}:
                entry.update(status="invalid", error="the chain under this camera belongs to another camera")
            if installation_key is not None:
                entry["signed_by_this_installation"] = result["public_key"] == installation_key
                if entry["status"] == "valid" and not entry["signed_by_this_installation"]:
                    entry.update(status="invalid", error="the chain was signed with a key that is not this installation's")
            cameras.append(entry)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return {"valid": False, "cameras": [], "error": f"chain.json is not a valid chain: {str(exc)[:200]}"}
    return {"valid": all(item["status"] != "invalid" for item in cameras), "cameras": cameras, "error": None}


HASH_CHUNK = 1024 * 1024


def member_digest(archive: zipfile.ZipFile, name: str) -> tuple[int, str]:
    """A member's size and SHA-256, read a piece at a time: a package's video is never held whole in memory (#359)."""
    digest, size = hashlib.sha256(), 0
    with archive.open(name) as member:
        while chunk := member.read(HASH_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def verify_evidence_bytes(package: bytes, expected_export_id: str | None = None, installation_key: str | None = None) -> dict[str, Any]:
    """``verify_evidence_file`` for a package already in memory (a small one, or a test's)."""
    return verify_evidence_file(io.BytesIO(package), expected_export_id, installation_key)


def read_integrity_manifest(archive: zipfile.ZipFile) -> tuple[list[str], dict[str, Any]]:
    """The member names and the integrity manifest of an uploaded package, once it has passed the checks made before any file is read: no
    path that is absolute or climbs out, no name given twice, no more uncompressed content than the limit, and one manifest of the known
    kind. Raises ValueError saying which failed. Shared by the evidence and case package verifiers (#387)."""
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
    return names, manifest


def verify_evidence_file(package: "str | os.PathLike[str] | IO[bytes]", expected_export_id: str | None = None, installation_key: str | None = None) -> dict[str, Any]:
    """Verify the evidence manifest against the exact uploaded ZIP member bytes, reading the package from a file.

    This deliberately does not extract files or validate that media is playable. Blocking: callers on an event loop run it in a thread.
    """
    try:
        with zipfile.ZipFile(package) as archive:
            names, manifest = read_integrity_manifest(archive)
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
                size, digest = member_digest(archive, name)
                entry = expected[name]
                matches = size == entry.get("size_bytes") and digest == entry.get("sha256")
                valid = valid and matches
                files.append({"path": name, "size_bytes": size, "sha256": digest, "matches": matches})
            chain = verify_chain_member(archive, installation_key) if "chain.json" in delivered else None
            return {"checksums_valid": valid, "files": files, "chain": chain,
                    "error": None if valid else "one or more file checksums do not match"}
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"checksums_valid": False, "files": [], "error": f"invalid evidence package: {str(exc)[:200]}"}


def _camera_video_filename(name: str, camera_id: str, index: int) -> str:
    readable = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-_").lower()[:48] or "camera"
    stable_suffix = re.sub(r"[^A-Za-z0-9]", "", camera_id).lower() or f"{index + 1:02d}"
    return f"camera-{readable}-{index + 1:02d}-{stable_suffix}.mp4"


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


def expire_exports(now: datetime | None = None) -> None:
    """Remove expired package bytes while retaining durable job metadata."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    with connect() as db:
        rows = db.execute("SELECT id,status,output_relative_path FROM exports WHERE status='completed' AND expires_at IS NOT NULL AND expires_at<=? AND output_relative_path IS NOT NULL", (moment,)).fetchall()
        for row in rows:
            try:
                path = export_output_path(row)
            except (ValueError, FileNotFoundError):
                path = None
            if path is not None and not unlink_if_unleased(path):
                continue
            db.execute("UPDATE exports SET output_relative_path=NULL,updated_at=? WHERE id=?", (moment, row["id"]))


def _ensure_export_space(directory: Path, requested_bytes: int = 0) -> None:
    if requested_bytes > MAX_EXPORT_PACKAGE_BYTES:
        raise RuntimeError("Evidence package exceeds the 2 GiB export limit")
    free = shutil.disk_usage(directory).free
    if free - requested_bytes < free_floor(directory):
        raise RuntimeError("Insufficient free storage for evidence export: the recordings volume's headroom must stay free")


def _has_audio(path: Path) -> bool:
    probe = shutil.which("ffprobe")
    if not probe:
        raise RuntimeError("ffprobe is unavailable; optional audio cannot be verified")
    try:
        result = subprocess.run([probe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        # The export's stored error was this exception's text, which includes the command line and the file's path (#386).
        raise RuntimeError("The recording's audio could not be checked in time") from None
    return result.returncode == 0 and bool(result.stdout.strip())


async def _render_audio(manager: "ExportManager", job_id: str, spans: list[dict[str, Any]],
                        duration: float, output: Path, grid_intervals: list[dict[str, Any]] | None = None) -> None:
    """Render a selected source's audio on output time, leaving missing spans silent."""
    if duration <= 0:
        raise RuntimeError("Audio export has no recorded duration")
    # One list of the spans that have audio, placed on output time, as the grid's tiles read their video (#356): it used to be one FFmpeg input
    # per file, all open at once. Spans without audio, and the time between spans, are silence.
    audio_presence: dict[Path, bool] = {}
    voiced: list[tuple[dict[str, Any], float]] = []
    for span in spans:
        source_path = await asyncio.to_thread(recording_file_path, span["segment"]["relative_path"])
        if source_path not in audio_presence:
            audio_presence[source_path] = await asyncio.to_thread(_has_audio, source_path)
        if audio_presence[source_path]:
            voiced.append((span, _union_output_time(span["source_start"], grid_intervals) if grid_intervals is not None else span["output_start"]))
    args = ["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:duration={duration:.6f}"]
    graph = [f"[0:a]atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[silence]"]
    if voiced:
        starts = await asyncio.to_thread(_first_frame_times, [recording_file_path(span["segment"]["relative_path"]) for span, _ in voiced], "a:0")
        manifest = output.with_suffix(".ffconcat")
        keep = _span_list(manifest, [span for span, _ in voiced], [start for _, start in voiced], duration,
                          inpoint_of=lambda span: starts.get(recording_file_path(span["segment"]["relative_path"]), 0.0) + _source_offset(span))
        args.extend(["-copyts", "-f", "concat", "-safe", "0", "-i", str(manifest)])
        graph.append(f"[1:a:0]aselect='{keep}',asetpts=PTS+{voiced[0][1]:.6f}/TB,aformat=channel_layouts=stereo,"
                     f"aresample=48000:async=1:first_pts=0,apad,atrim=duration={duration:.6f}[voice]")
        graph.append(f"[silence][voice]amix=inputs=2:duration=first:normalize=0,atrim=duration={duration:.6f}[out]")
    else:
        graph.append("[silence]anull[out]")
    script = output.with_suffix(".filter")
    script.write_text(";".join(graph), encoding="utf-8")
    args.extend(["-filter_complex_script", str(script), "-map", "[out]", "-c:a", "aac", "-b:a", "128k", "-y", str(output)])
    await manager._run_ffmpeg(job_id, args)
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("No playable audio track was produced")


async def _mux_audio(manager: "ExportManager", job_id: str, video: Path, audio: Path) -> None:
    muxed = video.with_name(video.stem + "-audio.mp4")
    await manager._run_ffmpeg(job_id, ["-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", "-shortest", "-y", str(muxed)])
    if not muxed.is_file() or muxed.stat().st_size == 0:
        raise RuntimeError("Audio could not be added to the evidence video")
    os.replace(muxed, video)


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


SEAM_TOLERANCE = timedelta(seconds=2)
CLIP_PADDING_SECONDS = 0.5


def _contiguous_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Segments as they follow each other in time.

    The recorder cuts one run of the camera into back-to-back files, but names each file after the whole second it was opened, while a file's length comes from
    its own timestamps. Neighbouring files then disagree by up to a second: read literally, the next file starts "before" the last ended, and the export skips that much
    real footage. A file of the same run, named by the whole second, that starts up to a couple of seconds before the last one ended is placed end to end instead. A gap is left as it is (it is reported
    as a gap, and the last frame is held), and so is a file from another run (the recorder reconnected): its times are its own.
    """
    placed: list[dict[str, Any]] = []
    for segment in sorted(segments, key=lambda item: (_parse_time(item["starts_at"]), int(item["id"]))):
        item = dict(segment)
        previous = placed[-1] if placed else None
        if previous is not None and PurePosixPath(previous["relative_path"]).parent == PurePosixPath(item["relative_path"]).parent:
            starts, ends, previous_end = _parse_time(item["starts_at"]), _parse_time(item["ends_at"]), _parse_time(previous["ends_at"])
            if starts.microsecond == 0 and timedelta(0) < previous_end - starts <= SEAM_TOLERANCE:  # The recorder's names are whole seconds; any other start is a time someone chose
                item["starts_at"], item["ends_at"] = previous_end.isoformat(), (previous_end + (ends - starts)).isoformat()
        placed.append(item)
    return placed


def _unique_output_spans(start: datetime, end: datetime, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim, order, and de-duplicate indexed source intervals for one camera."""
    cursor = start
    output_cursor = 0.0
    spans: list[dict[str, Any]] = []
    for segment in sorted(segments, key=lambda item: (_parse_time(item["starts_at"]), int(item["id"]))):
        source_start = max(start, cursor, _parse_time(segment["starts_at"]))
        source_end = min(end, _parse_time(segment["ends_at"]))
        if source_end <= source_start:
            continue
        duration = (source_end - source_start).total_seconds()
        spans.append({
            "segment": segment,
            "source_start": source_start,
            "source_end": source_end,
            "output_start": output_cursor,
            "output_end": output_cursor + duration,
            "gap_before": source_start > cursor,
        })
        cursor = source_end
        output_cursor += duration
    return spans


def _union_intervals(cameras: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    intervals = sorted((span["source_start"], span["source_end"]) for camera in cameras for span in camera["spans"])
    merged: list[tuple[datetime, datetime]] = []
    for left, right in intervals:
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    result: list[dict[str, Any]] = []
    cursor, output = start, 0.0
    for left, right in merged:
        if left > cursor:
            result.append({"source_start": cursor, "source_end": left, "output_start": output, "output_end": output, "skipped": True})
        duration = (right - left).total_seconds()
        result.append({"source_start": left, "source_end": right, "output_start": output, "output_end": output + duration, "skipped": False})
        output += duration
        cursor = right
    if cursor < end:
        result.append({"source_start": cursor, "source_end": end, "output_start": output, "output_end": output, "skipped": True})
    return result


def _union_output_time(at: datetime, intervals: list[dict[str, Any]]) -> float:
    for item in intervals:
        if not item["skipped"] and item["source_start"] <= at <= item["source_end"]:
            return item["output_start"] + (at - item["source_start"]).total_seconds()
    return sum((item["source_end"] - item["source_start"]).total_seconds() for item in intervals
               if not item["skipped"] and item["source_end"] <= at)


def _union_source_time(seconds: float, intervals: list[dict[str, Any]]) -> datetime | None:
    for index, item in enumerate(intervals):
        if item["skipped"]:
            continue
        if item["output_start"] <= seconds < item["output_end"]:
            return item["source_start"] + timedelta(seconds=max(0.0, seconds - item["output_start"]))
        next_output = next((candidate for candidate in intervals[index + 1:] if not candidate["skipped"]), None)
        if seconds == item["output_end"] and (next_output is None or next_output["output_start"] > seconds):
            return item["source_end"]
    return None


def _write_grid_subtitles(path: Path, camera_name: str, spans: list[dict[str, Any]], intervals: list[dict[str, Any]],
                          duration: float, display_settings: dict[str, str]) -> dict[str, Any]:
    safe_name = _overlay_name(camera_name) or "Selected camera"
    cues: list[tuple[float, float, list[str]]] = []
    events = [{"start": _union_output_time(span["source_start"], intervals),
               "end": _union_output_time(span["source_end"], intervals), "span": span} for span in spans]
    cursor = 0.0
    last_capture: datetime | None = None
    missing_cues: list[dict[str, Any]] = []
    def display(value: datetime) -> str:
        return format_display_datetime(value, display_settings["time_zone"], display_settings["date_format"], display_settings["clock_format"])
    for event in events:
        while cursor < event["start"]:
            cue_end = min(event["start"], cursor + 1.0)
            lines = ["{\\an5}" + safe_name, "No video available"]
            shared_time = _union_source_time(cursor, intervals)
            if shared_time:
                lines.append(f"Shared source time: {display(shared_time)}")
            if last_capture:
                lines.append(f"Last frame captured: {display(last_capture)}")
            cues.append((cursor, cue_end, lines))
            missing_cues.append({"output_start_seconds": round(cursor, 6),
                                 "shared_source_time": shared_time.isoformat() if shared_time else None,
                                 "last_frame_capture_time": last_capture.isoformat() if last_capture else None})
            cursor = cue_end
        span = event["span"]
        elapsed = 0.0
        while event["start"] + elapsed < event["end"]:
            source_time = span["source_start"] + timedelta(seconds=elapsed)
            cue_end = min(event["end"], event["start"] + elapsed + 1.0)
            lines = ["{\\an7}" + safe_name, display(source_time)]
            cues.append((event["start"] + elapsed, cue_end, lines))
            elapsed = cue_end - event["start"]
        cursor = event["end"]
        last_capture = span["source_end"]
    while cursor < duration:
        cue_end = min(duration, cursor + 1.0)
        lines = ["{\\an5}" + safe_name, "No video available"]
        shared_time = _union_source_time(cursor, intervals)
        if shared_time:
            lines.append(f"Shared source time: {display(shared_time)}")
        if last_capture:
            lines.append(f"Last frame captured: {display(last_capture)}")
        cues.append((cursor, cue_end, lines))
        missing_cues.append({"output_start_seconds": round(cursor, 6),
                             "shared_source_time": shared_time.isoformat() if shared_time else None,
                             "last_frame_capture_time": last_capture.isoformat() if last_capture else None})
        cursor = cue_end
    with path.open("w", encoding="utf-8") as subtitle:
        for number, (left, right, lines) in enumerate(cues, 1):
            if right > left:
                subtitle.write(f"{number}\n{_srt_time(left)} --> {_srt_time(right)}\n" + "\n".join(lines) + "\n\n")
    return {"source_to_output_intervals": [{"source_start": span["source_start"].isoformat(), "source_end": span["source_end"].isoformat(),
             "output_start_seconds": round(_union_output_time(span["source_start"], intervals), 6),
             "output_end_seconds": round(_union_output_time(span["source_end"], intervals), 6)} for span in spans],
            "missing_tile_cues": missing_cues}


def _span_list(path: Path, spans: list[dict[str, Any]], offsets: list[float], end: float, *, inpoint_of: Any) -> str:
    """Write a camera's spans as one ffconcat list placed on the output timeline, and return the selection that keeps only their frames.

    Each entry's ``duration`` is the time to the next span's output start, so the list's timestamps leave the camera's gaps empty (the tile
    then holds the last frame, as before). The concat demuxer still hands back the frames from each file's keyframe to its inpoint, timed
    just before the entry; read with ``-copyts`` they keep the list's own times, and the returned ``select`` expression keeps only each
    span's own window (#356)."""
    first = offsets[0]
    lines, windows = ["ffconcat version 1.0\n"], []
    for index, span in enumerate(spans):
        start = offsets[index] - first
        span_duration = (span["source_end"] - span["source_start"]).total_seconds()
        next_start = (offsets[index + 1] if index + 1 < len(spans) else end) - first
        inpoint = inpoint_of(span)
        lines.append(_ffconcat_quote(recording_file_path(span["segment"]["relative_path"])))
        lines.append(f"inpoint {inpoint:.6f}\noutpoint {inpoint + span_duration:.6f}\nduration {max(span_duration, next_start - start):.6f}\n")
        windows.append(f"gte(t,{start:.6f})*lt(t,{start + span_duration:.6f})")
    path.write_text("".join(lines), encoding="utf-8")
    return "+".join(windows)


def _source_offset(span: dict[str, Any]) -> float:
    return max(0.0, (span["source_start"] - _parse_time(span["segment"]["starts_at"])).total_seconds())


def _first_frame_times(paths: list[Path], stream: str = "v:0") -> dict[Path, float]:
    """When each file's first video frame is, on its own clock. A file cut from a camera's stream often starts a little after zero (its
    B-frames); a list entry's inpoint is on that clock, so it is added to the span's offset, as ``-ss`` on each file used to do (#356)."""
    from concurrent.futures import ThreadPoolExecutor

    probe = shutil.which("ffprobe")

    def first(path: Path) -> float:
        if not probe:
            return 0.0
        try:
            result = subprocess.run([probe, "-v", "error", "-select_streams", stream, "-show_entries", "stream=start_time", "-of", "csv=p=0", str(path)],
                                    capture_output=True, text=True, timeout=15, check=False)
            return max(0.0, float(result.stdout.strip().splitlines()[0]))
        except (subprocess.TimeoutExpired, ValueError, IndexError):
            return 0.0

    unique = list(dict.fromkeys(paths))
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(zip(unique, pool.map(first, unique)))


async def _render_grid_tile(manager: "ExportManager", job_id: str, work_dir: Path, tile_index: int, camera: dict[str, Any] | None,
                            intervals: list[dict[str, Any]], duration: float, size: tuple[int, int], subtitle_path: Path, hardware: bool) -> Path:
    """One tile of the grid as its own video, from one list of its camera's files: memory stays flat however many files there are (#356)."""
    tile_width, tile_height = size
    encoder = acceleration.encoder(crf=18, speed="ultrafast") if hardware else acceleration.build(None, crf=18, speed="ultrafast")
    args = ["-hide_banner", "-loglevel", "error", *encoder.pre, "-f", "lavfi", "-i", f"color=c=black:s={tile_width}x{tile_height}:r=15:d={duration:.6f}"]
    spans = camera["spans"] if camera else []
    graph: list[str] = []
    composed = "0:v"
    if spans:
        offsets = [_union_output_time(span["source_start"], intervals) for span in spans]
        manifest = work_dir / f"grid-tile-{tile_index}.ffconcat"
        starts = await asyncio.to_thread(_first_frame_times, [recording_file_path(span["segment"]["relative_path"]) for span in spans])
        keep = _span_list(manifest, spans, offsets, duration,
                          inpoint_of=lambda span: starts.get(recording_file_path(span["segment"]["relative_path"]), 0.0) + _source_offset(span))
        args.extend(["-copyts", "-f", "concat", "-safe", "0", "-i", str(manifest)])
        mask = f"{privacy.drawbox_filter(camera['privacy_zones'])}," if camera.get("privacy_zones") else ""
        graph.append(f"[1:v]select='{keep}',setpts=PTS+{offsets[0]:.6f}/TB,{mask}fps=15,scale={tile_width}:{tile_height}:force_original_aspect_ratio=decrease,"
                     f"pad={tile_width}:{tile_height}:(ow-iw)/2:(oh-ih)/2,setsar=1[clip]")
        # Before the camera's first frame the tile is black; after each span, and after the last, its last frame is held.
        graph.append("[0:v][clip]overlay=eof_action=repeat:shortest=0[composed]")
        composed = "composed"
    graph.append(f"[{composed}]{_subtitle_filter_path(subtitle_path, tile_width, tile_height, OVERLAY_MIN_TILE_TEXT_PX)}" + (f",{encoder.vf_tail}" if encoder.vf_tail else "") + "[out]")
    script = work_dir / f"grid-tile-{tile_index}.filter"
    script.write_text(";".join(graph), encoding="utf-8")  # In a file: with hundreds of spans the description is too long for a command line
    output = work_dir / f"grid-tile-{tile_index}.mp4"
    args.extend(["-filter_complex_script", str(script), "-map", "[out]", "-an", "-r", "15", *encoder.codec, "-t", f"{duration:.6f}", "-y", str(output)])
    await manager._run_ffmpeg(job_id, args)
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("No playable video was produced for a grid tile")
    return output


async def _render_grid(manager: "ExportManager", job_id: str, work_dir: Path, cameras: list[dict[str, Any]],
                       tile_camera_ids: list[str | None], intervals: list[dict[str, Any]], resolution: str,
                       display_settings: dict[str, str], hardware: bool = False) -> Path:
    """The synchronized grid, a tile at a time (#356): each tile is rendered on its own from one list of its camera's files, then the
    finished tiles are put side by side. FFmpeg used to open and decode every file of every camera at once (about 64 MB each), and above
    about 400 files the command was too long to start."""
    encoder = acceleration.encoder(crf=26, speed="ultrafast") if hardware else acceleration.build(None, crf=26, speed="ultrafast")
    width, height = (1920, 1080) if resolution == "1080p" else (3840, 2160)
    columns = int(len(tile_camera_ids) ** 0.5)
    tile_width, tile_height = width // columns, height // columns
    duration = sum((item["source_end"] - item["source_start"]).total_seconds() for item in intervals if not item["skipped"])
    by_id = {camera["camera_id"]: camera for camera in cameras}
    tiles: list[Path] = []
    for tile_index, camera_id in enumerate(tile_camera_ids):
        camera = by_id.get(camera_id) if camera_id else None
        subtitle_path = work_dir / f"grid-tile-{tile_index}.srt"
        overlay_mapping = _write_grid_subtitles(subtitle_path, camera["name"] if camera else "Selected camera", camera["spans"] if camera else [], intervals, duration, display_settings)
        if camera:
            camera["grid_overlay"] = overlay_mapping
        tiles.append(await _render_grid_tile(manager, job_id, work_dir, tile_index, camera, intervals, duration, (tile_width, tile_height), subtitle_path, hardware))
        if job_id in getattr(manager, "_cancelled", ()):  # Between tiles, so a cancelled grid does not render the rest
            raise ExportCancelled()
    args = ["-hide_banner", "-loglevel", "error", *encoder.pre]
    for tile in tiles:
        args.extend(["-i", str(tile)])
    graph: list[str] = []
    tile_outputs = [f"[{index}:v]" for index in range(len(tiles))]
    layout = "|".join(f"{index % columns * tile_width}_{index // columns * tile_height}" for index in range(len(tile_outputs)))
    gaps = work_dir / "grid-gaps.srt"
    with gaps.open("w", encoding="utf-8") as subtitle:
        cue = 1
        for item in intervals:
            if not item["skipped"] or item["source_start"] == item["source_end"]:
                continue
            left, right = max(0.0, item["output_start"] - 0.4), min(duration, item["output_start"] + 1.6)
            if right > left:
                subtitle.write(f"{cue}\n{_srt_time(left)} --> {_srt_time(right)}\n{{\\an2}}Recording gap skipped\n{format_display_datetime(item['source_start'], display_settings['time_zone'], display_settings['date_format'], display_settings['clock_format'])} – {format_display_datetime(item['source_end'], display_settings['time_zone'], display_settings['date_format'], display_settings['clock_format'])}\n\n")
                cue += 1
    final_filter = f"{tile_outputs[0]}null" if len(tile_outputs) == 1 else "".join(tile_outputs) + f"xstack=inputs={len(tile_outputs)}:layout={layout}:fill=black"
    # Gap notices sit bottom-centre of the whole frame, apart from every tile's top-left label.
    graph.append(f"{final_filter},{_subtitle_filter_path(gaps, width, height)}" + (f"[gridframe];[gridframe]{encoder.vf_tail}[out]" if encoder.vf_tail else "[out]"))
    output = work_dir / "grid.mp4"
    script = work_dir / "grid.filter"
    script.write_text(";".join(graph), encoding="utf-8")
    args.extend(["-filter_complex_script", str(script), "-map", "[out]", "-an", "-r", "15", *encoder.codec, "-movflags", "+faststart", "-t", f"{duration:.6f}", "-y", str(output)])
    await manager._run_ffmpeg(job_id, args)
    for tile in tiles:
        tile.unlink(missing_ok=True)  # The tiles were steps on the way: only the grid is packaged
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("No playable synchronized grid video was produced")
    return output


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def _write_source_time_subtitles(
    path: Path, camera_name: str, spans: list[dict[str, Any]], display_settings: dict[str, str],
) -> list[str]:
    """Write once-per-second source-clock labels on the compressed output clock."""
    safe_name = _overlay_name(camera_name)
    time_zone = display_settings["time_zone"]
    date_format = display_settings["date_format"]
    clock_format = display_settings["clock_format"]
    first_samples: list[str] = []
    cue_number = 1
    with path.open("w", encoding="utf-8") as subtitle:
        for span in spans:
            duration = (span["source_end"] - span["source_start"]).total_seconds()
            if duration <= 0:
                continue
            first_samples.append(format_display_datetime(span["source_start"], time_zone, date_format, clock_format))
            elapsed = 0.0
            while elapsed < duration:
                cue_end = min(duration, elapsed + 1.0)
                source_time = span["source_start"] + timedelta(seconds=elapsed)
                lines = ["{\\an7}" + safe_name, format_display_datetime(source_time, time_zone, date_format, clock_format)]
                subtitle.write(f"{cue_number}\n{_srt_time(span['output_start'] + elapsed)} --> {_srt_time(span['output_start'] + cue_end)}\n")
                subtitle.write("\n".join(lines) + "\n\n")
                cue_number += 1
                if span["gap_before"] and elapsed < 2.0:
                    # Separate bottom-centre cue: never stacked into the name/time label.
                    subtitle.write(f"{cue_number}\n{_srt_time(span['output_start'] + elapsed)} --> {_srt_time(span['output_start'] + cue_end)}\n")
                    subtitle.write("{\\an2}Recording gap skipped\n\n")
                    cue_number += 1
                elapsed = cue_end
    return first_samples


# Burned-in overlays are sized from the frame they are drawn on. FFmpeg converts SRT
# to ASS with a 384x288 script, and libass renders text about 0.72 x Fontsize x H/288
# pixels tall, so Fontsize = text_px x 400 / H. 2.5% of the frame height gives about
# 18 px at 720p, 27 px at 1080p and 54 px at 4K; dense grid tiles keep a readable floor.
OVERLAY_TEXT_RATIO = 0.025
OVERLAY_MIN_TILE_TEXT_PX = 12
MAX_OVERLAY_NAME_CHARACTERS = 40


def _overlay_text_px(height: int, minimum: int = 0) -> float:
    return max(OVERLAY_TEXT_RATIO * height, minimum)


def _subtitle_filter_path(path: Path, width: int, height: int, minimum_text_px: int = 0) -> str:
    """The burn-in filter for a subtitle file, or a pass-through when it has no cues.

    FFmpeg's subtitles filter refuses an empty file, which would fail an export
    whose interval simply has no skipped gaps. Placement comes from each cue's
    \\an override; the style only sets size, padding and colours.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return "null"
    text_px = _overlay_text_px(height, minimum_text_px)
    font = text_px * 400 / height
    margin_px = text_px * 0.6
    margin_horizontal = margin_px * 384 / width
    margin_vertical = margin_px * 288 / height
    padding = text_px * 0.2 * 288 / height
    style = (
        f"Fontsize={font:.2f},BorderStyle=3,Outline={padding:.2f},Shadow=0,"
        f"MarginL={margin_horizontal:.2f},MarginR={margin_horizontal:.2f},MarginV={margin_vertical:.2f},"
        "PrimaryColour=&H00FFFFFF,BackColour=&H80000000"
    )
    rendered = path.as_posix().replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return f"subtitles=filename='{rendered}':force_style='{style}'"


def _overlay_name(camera_name: str) -> str:
    """A camera name safe for subtitle markup and short enough for a corner label.

    Names longer than MAX_OVERLAY_NAME_CHARACTERS are cut with an ellipsis; the
    full name stays in the package metadata.
    """
    safe = " ".join(camera_name.replace("<", "[").replace(">", "]").replace("{", "(").replace("}", ")").replace("\\", "/").split())
    if len(safe) > MAX_OVERLAY_NAME_CHARACTERS:
        safe = safe[:MAX_OVERLAY_NAME_CHARACTERS - 1].rstrip() + "…"
    return safe


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
        "display_settings": json.loads(row["display_settings_json"]),
        "tile_camera_ids": json.loads(row["tile_camera_ids_json"]),
        "grid_resolution": row["grid_resolution"],
        "include_audio": bool(row["include_audio"]),
        "audio_camera_id": row["audio_camera_id"],
        "progress": int(row["progress"]),
        "expires_at": row["expires_at"],
        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
        "error": row["error"],
        "download_url": f"/api/exports/{row['id']}/download" if row["status"] == "completed" and row["output_relative_path"] and (not row["expires_at"] or _parse_time(row["expires_at"]) > datetime.now(timezone.utc)) else None,
        "cancel_requested": bool(row["cancel_requested"]),
        "retry_of": row["retry_of"],
        "redaction_review_id": row["redaction_review_id"],
        "dewarp": {camera_id: entry["view"] for camera_id, entry in json.loads(row["dewarp_json"] or "{}").items()},
        "dewarp_lenses": {camera_id: entry["lens"] for camera_id, entry in json.loads(row["dewarp_json"] or "{}").items()},
    }


@dataclass
class _ExportJob:
    """What one queued export asked for, and where its files go."""
    id: str
    requester: int
    camera_ids: list[str]
    output_modes: list[str]
    display_settings: dict[str, Any]
    tile_camera_ids: list[str | None]
    grid_resolution: str
    include_audio: bool
    audio_camera_id: str | None
    redaction_review_id: str | None
    dewarp_plan: dict[str, Any]
    start: datetime
    end: datetime
    work_dir: Path
    final_path: Path
    partial_path: Path


def _claim_export(job_id: str) -> "_ExportJob | None":
    """Take a queued job (the first to claim it wins) and read what it asked for; None when it is not queued any more."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM exports WHERE id=? AND status='queued'", (job_id,)).fetchone()
        if row is None:
            return
        claimed = db.execute("UPDATE exports SET status='running',progress=1,updated_at=? WHERE id=? AND status='queued'", (datetime.now(timezone.utc).isoformat(), job_id))
        if claimed.rowcount != 1:
            return
        camera_ids = json.loads(row["camera_ids_json"])
        output_modes = json.loads(row["output_modes_json"])
        display_settings = json.loads(row["display_settings_json"])
        tile_camera_ids = json.loads(row["tile_camera_ids_json"])
        grid_resolution = row["grid_resolution"]
        include_audio = bool(row["include_audio"])
        audio_camera_id = row["audio_camera_id"]
        redaction_review_id = row["redaction_review_id"]
        dewarp_plan = json.loads(row["dewarp_json"] or "{}")
        requester = row["requested_by"]
        start, end = _parse_time(row["requested_start"]), _parse_time(row["requested_end"])
    return _ExportJob(
        id=job_id, requester=requester, camera_ids=camera_ids, output_modes=output_modes, display_settings=display_settings, tile_camera_ids=tile_camera_ids,
        grid_resolution=grid_resolution, include_audio=include_audio, audio_camera_id=audio_camera_id, redaction_review_id=redaction_review_id,
        dewarp_plan=dewarp_plan, start=start, end=end, work_dir=recordings_path() / EXPORT_DIRECTORY / f".{job_id}.work",
        final_path=recordings_path() / EXPORT_DIRECTORY / f"{job_id}.zip", partial_path=recordings_path() / EXPORT_DIRECTORY / f"{job_id}.zip.partial",
    )


def _reserve_sources(job: "_ExportJob", acquired: list[Path]) -> tuple[list[dict[str, Any]], int]:
    """Read what is to be exported and install the playback lease on every source, under the write lock.

    One synchronous call, run in a worker thread: the lock is never held while the event loop waits for something. Anything else on the loop that writes to the
    database would otherwise wait for a lock whose holder cannot continue until the loop is free (a deadlock that ends only at the busy timeout).
    """
    camera_ids = job.camera_ids
    output_modes = job.output_modes
    include_audio = job.include_audio
    audio_camera_id = job.audio_camera_id
    start = job.start
    end = job.end
    fetch_cold_sources(camera_ids, start, end)  # Downloads happen here, before the lock (#358)
    with connect() as db:
        # Retention also takes this write lock before unlinking a segment.
        # Keep it until the in-memory lease is installed for every source.
        db.execute("BEGIN IMMEDIATE")
        per_camera: list[dict[str, Any]] = []
        total_segments = 0
        total_source_bytes = 0
        for index, camera_id in enumerate(camera_ids):
            camera = db.execute("SELECT id,name,audio_supported,audio_enabled FROM cameras WHERE id=?", (camera_id,)).fetchone()
            if camera is None:
                raise RuntimeError("A selected camera was removed before export started")
            segments = _contiguous_segments([dict(item) for item in db.execute(
                "SELECT id,relative_path,starts_at,ends_at,size_bytes FROM recording_segments "
                "WHERE camera_id=? AND starts_at<? AND ends_at>? AND unavailable_at IS NULL ORDER BY starts_at",
                (camera_id, end.isoformat(), start.isoformat()),
            ).fetchall()])
            # Privacy masks: burn in the mask over the whole camera if any of its footage needs it.
            zones: list[dict[str, float]] = []
            for segment in segments:
                zones = zones or privacy.server_zones(db, camera_id, _parse_time(segment["starts_at"]))
            per_camera.append({"camera_id": camera_id, "name": camera["name"], "index": index, "segments": segments, "privacy_zones": zones,
                               "audio_supported": bool(camera["audio_supported"]), "audio_enabled": bool(camera["audio_enabled"]),
                               "spans": _unique_output_spans(start, end, segments)})
            total_segments += len(segments)
            total_source_bytes += sum(int(segment["size_bytes"]) for segment in segments)
        if not total_segments:
            raise RuntimeError("No indexed footage is available in the requested interval")
        if total_segments > MAX_EXPORT_SEGMENTS or total_source_bytes > MAX_EXPORT_SOURCE_BYTES:
            raise RuntimeError("Export source exceeds the supported size or segment limit")
        too_many = _too_many_files(total_segments)
        if too_many:
            raise RuntimeError(too_many)
        if include_audio:
            eligible_audio = [camera for camera in per_camera if camera["audio_supported"] and camera["audio_enabled"]]
            if "grid" in output_modes:
                source = next((camera for camera in per_camera if camera["camera_id"] == audio_camera_id), None)
                if source is None or not source["audio_supported"] or not source["audio_enabled"]:
                    raise RuntimeError("Grid audio source must have administrator-enabled audio")
            elif not eligible_audio:
                raise RuntimeError("Audio export requires at least one administrator-enabled camera audio source")
        for camera in per_camera:
            for segment in camera["segments"]:
                path = recording_file_path(segment["relative_path"])
                if not path.is_file():
                    raise RuntimeError("A source recording is unavailable; no partial export was produced")
                acquire_playback(path)
                acquired.append(path)
    return per_camera, total_segments


def _base_metadata(job: "_ExportJob") -> dict[str, Any]:
    """The manifest as far as it can be written before any video is made."""
    job_id = job.id
    requester = job.requester
    output_modes = job.output_modes
    display_settings = job.display_settings
    tile_camera_ids = job.tile_camera_ids
    grid_resolution = job.grid_resolution
    include_audio = job.include_audio
    audio_camera_id = job.audio_camera_id
    start = job.start
    end = job.end
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "package_type": "nvr_evidence",
        "export_id": job_id,
        "audit_reference": {"resource_type": "export", "resource_id": job_id},
        "requested_by_user_id": requester,
        "requested_interval": {"start": start.isoformat(), "end": end.isoformat()},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_modes": output_modes,
        "display_settings": display_settings,
        "tile_camera_ids": tile_camera_ids,
        "grid_resolution": grid_resolution,
        "audio": {"included": include_audio, "grid_camera_id": audio_camera_id if "grid" in output_modes else None,
                  "individual_policy": "Each administrator-enabled camera contributes its own recorded audio; other individual videos remain silent" if include_audio else "omitted",
                  "policy": "individual outputs use each camera's own recorded audio; grid uses only the selected camera and is silent when that source has no recorded audio" if include_audio else "Audio omitted by default"},
        "timestamp_provenance": "All timestamps are ISO 8601 with explicit UTC offsets and normalized to UTC. Recording intervals come from the NVR segment index; camera clock accuracy and timestamp authenticity are not independently attested.",
        "cameras": [],
        "omitted_cameras": [],
        "notes": ["Audio is omitted unless requested; individual outputs use the camera's own recorded audio, while grid output uses the selected camera source and silence for missing spans.", "Individual camera videos omit each camera's missing intervals. The synchronized grid follows the union of selected camera recordings and compresses only intervals where every selected camera is missing footage.", "Burned camera and time labels use the site display settings captured when this export was requested; labels show source time before gap compression.", "SHA-256 detects changes relative to this manifest but is not a digital signature and does not establish who created the package or when."],
    }
    return metadata


def _redaction_regions(job: "_ExportJob", per_camera: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
    """The redaction review's boxes for this export, checked against the footage and masks as they are now; adds its entry to the manifest."""
    redaction_review_id = job.redaction_review_id
    redaction_regions: dict[str, Any] = {}
    if redaction_review_id:
        with connect() as db:
            redaction_regions, metadata["redaction"] = redaction.manifest_entry(db, redaction_review_id)
        # The review's boxes are keyed to the footage it analysed: if that, or a privacy mask, has changed since, they would land on the wrong frames.
        if redaction_regions.get("fingerprint") != redaction.fingerprint((camera["camera_id"], camera) for camera in per_camera if camera["spans"]):
            raise RuntimeError("The footage or privacy masks changed since the redaction review; review the range again")
    return redaction_regions


def _grid_timeline(job: "_ExportJob", union: list[dict[str, Any]]) -> dict[str, Any]:
    """The grid's place in time: where each stretch of source footage lands in the output and which gaps are compressed. Sets output_start and output_end on each interval."""
    tile_camera_ids = job.tile_camera_ids
    grid_resolution = job.grid_resolution
    compressed = 0.0
    source_to_output = []
    global_gaps = []
    for interval in union:
        if interval["skipped"]:
            global_gaps.append({"source_start": interval["source_start"].isoformat(), "source_end": interval["source_end"].isoformat(),
                                "output_transition_seconds": round(compressed, 6),
                                "duration_seconds": round((interval["source_end"] - interval["source_start"]).total_seconds(), 6)})
        else:
            interval["output_start"] = compressed
            interval["output_end"] = compressed + (interval["source_end"] - interval["source_start"]).total_seconds()
            source_to_output.append({"source_start": interval["source_start"].isoformat(), "source_end": interval["source_end"].isoformat(),
                                     "output_start_seconds": round(compressed, 6), "output_end_seconds": round(interval["output_end"], 6)})
            compressed = interval["output_end"]
    return {"video_file": "grid.mp4", "resolution": grid_resolution, "tile_camera_ids": tile_camera_ids,
                         "duration_seconds": round(compressed, 6), "source_to_output_intervals": source_to_output,
                         "global_gaps_compressed": global_gaps}


def _chain_extract(camera_id: str, segment_ids: list[int]) -> dict[str, Any] | None:
    with connect() as db:
        return footage_chain.extract(db, camera_id, segment_ids)


async def _signed_chain(job: "_ExportJob", per_camera: list[dict[str, Any]], metadata: dict[str, Any]) -> Path:
    """The signed footage chain for the sources used, written beside the videos; notes in the manifest which cameras' footage was signed."""
    work_dir = job.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    # Signed footage: each source segment's hash now, and the signed chain covering it, for offline checks.
    # Catch the chain up first, so footage recorded moments ago is covered by a signature too.
    while await footage_chain.append_pending():
        pass
    await asyncio.to_thread(footage_chain.sign_heads)
    chain_package: dict[str, Any] = {"schema_version": 1, "algorithm": "Ed25519", "cameras": []}
    for camera in per_camera:
        sources = [{"segment_id": int(segment["id"]), "relative_path": segment["relative_path"],
                    "sha256": await asyncio.to_thread(footage_chain.file_sha256, recording_file_path(segment["relative_path"]))}
                   for segment in camera["segments"]]
        extract = await asyncio.to_thread(_chain_extract, camera["camera_id"], [source["segment_id"] for source in sources])
        chain_package["cameras"].append({"camera_id": camera["camera_id"], "sources": sources, "chain": extract})
    chain_path = work_dir / "chain.json"
    await asyncio.to_thread(chain_path.write_text, json.dumps(chain_package, indent=2, sort_keys=True), "utf-8")
    metadata["signed_footage"] = {camera["camera_id"]: ("signed" if camera["chain"] else "not_signed") for camera in chain_package["cameras"]}
    return chain_path


def _record_completed(job: "_ExportJob", metadata: dict[str, Any], expires_at: str, total_segments: int) -> None:
    """Mark the export completed and audit it, unless it was cancelled in the meantime."""
    job_id = job.id
    requester = job.requester
    completed_at = datetime.now(timezone.utc)
    # The hash of the file handed out: what a later check compares an uploaded package with, since the list inside a package is not signed (#363).
    package_sha256 = footage_chain.file_sha256(job.final_path)
    with connect() as db:
        updated = db.execute("UPDATE exports SET status='completed',progress=100,expires_at=?,metadata_json=?,output_relative_path=?,package_sha256=?,updated_at=?,completed_at=?,error=NULL WHERE id=? AND status='running' AND cancel_requested=0",
                   (expires_at, json.dumps(metadata, separators=(",", ":")), f"{EXPORT_DIRECTORY}/{job_id}.zip", package_sha256, completed_at.isoformat(), completed_at.isoformat(), job_id))
        if updated.rowcount != 1:
            raise ExportCancelled()
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (requester, "export_completed", "export", job_id, json.dumps({"camera_count": len(metadata["cameras"]), "omitted_camera_count": len(metadata["omitted_cameras"]), "segment_count": total_segments})))


def _record_cancelled(job: "_ExportJob") -> None:
    """A cancelled export leaves no files and is recorded as cancelled."""
    job_id = job.id
    requester = job.requester
    final_path = job.final_path
    partial_path = job.partial_path
    partial_path.unlink(missing_ok=True)
    final_path.unlink(missing_ok=True)
    with connect() as db:
        db.execute("UPDATE exports SET status='cancelled',progress=0,cancel_requested=0,error=NULL,updated_at=?,completed_at=? WHERE id=? AND status='running'", (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), job_id))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)", (requester, "export_cancelled", "export", job_id, "{}"))


def _record_failed(job: "_ExportJob", exc: Exception) -> None:
    """A failed export leaves no files and says why, up to 500 characters."""
    job_id = job.id
    requester = job.requester
    final_path = job.final_path
    partial_path = job.partial_path
    partial_path.unlink(missing_ok=True)
    final_path.unlink(missing_ok=True)
    with connect() as db:
        db.execute("UPDATE exports SET status='failed',error=?,updated_at=?,completed_at=? WHERE id=? AND status='running'", (str(exc)[:500], datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), job_id))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)", (requester, "export_failed", "export", job_id, json.dumps({"reason": str(exc)[:500]})))


class ExportManager:
    """Persistent, single-worker export queue with bounded queued work."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()
        self._stopping = False
        self._expiry_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._case_task: asyncio.Task[None] | None = None
        self._review_task: asyncio.Task[None] | None = None
        self._pending: set[str] = set()
        self._running: str | None = None

    async def start(self) -> None:
        max_open_sources()  # Raise this process's open-file limit now, before any export holds its sources open (#356)
        recover_interrupted_exports()
        redaction.recover()
        expire_exports()
        with connect() as db:
            pending = [row[0] for row in db.execute("SELECT id FROM exports WHERE status='queued' ORDER BY created_at")]
        self._stopping = False
        self._worker = asyncio.create_task(self._run(), name="nvr-export-worker")
        self._expiry_task = asyncio.create_task(self._expire_loop(), name="nvr-export-expiry")
        self._poll_task = asyncio.create_task(self._poll_loop(), name="nvr-export-poll")
        for job_id in pending:
            self.enqueue(job_id)

    def _progress(self, job_id: str, percent: int) -> None:
        with connect() as db:
            db.execute("UPDATE exports SET progress=?,updated_at=? WHERE id=? AND status='running'", (max(0, min(99, int(percent))), datetime.now(timezone.utc).isoformat(), job_id))

    def _advance_progress(self, job_id: str, moving: bool) -> None:
        """One step of the figure shown while a camera's video is encoded, up to 84 percent."""
        with connect() as db:
            row = db.execute("SELECT progress FROM exports WHERE id=?", (job_id,)).fetchone()
        current = int(row[0]) if row else 0
        self._progress(job_id, max(current, min(84, current + (1 if moving else 0))))

    async def _expire_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            for expire in (expire_exports, redaction.expire_reviews):
                try:
                    expire()
                except (sqlite3.Error, OSError):
                    logging.getLogger(__name__).warning("Expired exports or reviews could not be cleared this time", exc_info=True)

    async def _poll_loop(self) -> None:
        """Pick up queued jobs and cancel requests even when a nudge was lost."""
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                with connect() as db:
                    queued = [row[0] for row in db.execute("SELECT id FROM exports WHERE status='queued' ORDER BY created_at")]
                    cancelling = {row[0] for row in db.execute("SELECT id FROM exports WHERE status='running' AND cancel_requested=1")}
                    running = [row[0] for row in db.execute("SELECT id FROM exports WHERE status='running'")]
                    if self._running is None and running and not self._stopping:
                        # Nothing is being rendered, yet a job says it is: its end could not be recorded (#357). Say so rather than leave it for ever.
                        moment = datetime.now(timezone.utc)
                        now = moment.isoformat()
                        for row in db.execute("SELECT id,requested_by,updated_at FROM exports WHERE status='running'").fetchall():
                            if row["id"] == self._running or not _silent_for(row["updated_at"], moment, ORPHAN_AFTER_SECONDS):
                                continue  # A job that showed life a moment ago is not called abandoned on one look
                            db.execute("UPDATE exports SET status='failed',error=?,updated_at=?,completed_at=? WHERE id=? AND status='running'", ("Export was interrupted by an internal error; start it again", now, now, row["id"]))
                            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)", (row["requested_by"], "export_failed", "export", row["id"], '{"reason":"worker_error"}'))
            except sqlite3.Error:
                continue
            if (self._worker is None or self._worker.done()) and not self._stopping:
                logging.getLogger(__name__).error("The export worker had ended; starting it again")
                self._worker = asyncio.create_task(self._run(), name="nvr-export-worker")
            for job_id in queued:
                self.enqueue(job_id)
            if self._case_task is None or self._case_task.done():
                # Case packages: start each clip's export, assemble when they are done. Off the loop, so a long assembly does not hold up the queue or cancels.
                self._case_task = asyncio.create_task(self._advance_cases(), name="nvr-case-packages")
            if self._review_task is None or self._review_task.done():
                # Redaction reviews analyse in their own task, one at a time, so a long analysis never holds up an export.
                self._review_task = asyncio.create_task(redaction.advance_reviews(), name="nvr-redaction-reviews")
            if self._running in cancelling and self._running not in self._cancelled:
                await self.cancel(self._running)

    async def _advance_cases(self) -> None:
        try:
            from . import cases
            await asyncio.to_thread(cases.advance_packages)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - tried again on the next poll; never ends the poll loop
            logging.getLogger(__name__).exception("Case packages could not be advanced")

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._case_task, self._review_task):
            if task:
                task.cancel()
        self._case_task = self._review_task = None
        for task in (self._expiry_task, self._poll_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._expiry_task = self._poll_task = None
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
        if job_id in self._pending or job_id == self._running:
            return
        self._pending.add(job_id)
        self._queue.put_nowait(job_id)

    async def cancel(self, job_id: str) -> None:
        self._cancelled.add(job_id)
        process = self._processes.get(job_id)
        if process and process.returncode is None:
            process.terminate()

    async def _run(self) -> None:
        while True:
            job_id = await self._queue.get()
            self._pending.discard(job_id)
            try:
                if self._stopping:
                    return
                with connect() as db:
                    row = db.execute("SELECT status FROM exports WHERE id=?", (job_id,)).fetchone()
                if not row or row["status"] != "queued":
                    continue
                self._running = job_id
                await self._execute(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one job's error (the database busy while it was claimed or finished) must not end the worker (#357)
                # A job still queued is picked up again by the poll; one left "running" with nothing running is failed by the poll.
                logging.getLogger(__name__).exception("Export %s could not be run or finished; the export worker carries on", job_id)
            finally:
                self._running = None
                self._queue.task_done()

    async def _execute(self, job_id: str) -> None:
        # The export worker's database writes wait for the lock: made in a thread, a wait never stops the worker's heartbeat.
        job = await asyncio.to_thread(_claim_export, job_id)
        if job is None:
            return
        acquired: list[Path] = []
        try:
            if job_id in self._cancelled:
                raise ExportCancelled()
            job.work_dir.mkdir(parents=True, exist_ok=False)
            job.final_path.parent.mkdir(parents=True, exist_ok=True)
            _ensure_export_space(job.final_path.parent)
            per_camera, total_segments = await asyncio.to_thread(_reserve_sources, job, acquired)
            metadata = _base_metadata(job)
            redaction_regions = _redaction_regions(job, per_camera, metadata)
            await asyncio.to_thread(self._progress, job_id, 10)
            union = _union_intervals(per_camera, job.start, job.end) if "grid" in job.output_modes else []
            if union:
                metadata["grid"] = _grid_timeline(job, union)
            for camera in per_camera:
                if job_id in self._cancelled:
                    raise ExportCancelled()
                await self._render_camera(job, camera, redaction_regions, metadata, len(per_camera))
            if "grid" in job.output_modes:
                await self._render_grid_output(job, per_camera, union, metadata)
            chain_path = await _signed_chain(job, per_camera, metadata)
            with connect() as db:
                kept = timedelta(days=retention_days(db))
            expires_at = (datetime.now(timezone.utc) + kept).isoformat()
            metadata["expires_at"] = expires_at
            # Hashing and copying up to 2 GiB into the zip, and hashing the result: in a thread. On the loop, the exporter's sign of life
            # stopped for as long as it took, and past 30 seconds the supervisor ended the exporter with the export at 85 to 99 percent (#359).
            await asyncio.to_thread(self._write_package, job, metadata, chain_path)
            await asyncio.to_thread(_record_completed, job, metadata, expires_at, total_segments)
        except ExportCancelled:
            await asyncio.to_thread(_record_cancelled, job)
        except asyncio.CancelledError:
            job.partial_path.unlink(missing_ok=True)
            job.final_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            await asyncio.to_thread(_record_failed, job, exc)
        finally:
            self._processes.pop(job_id, None)
            self._cancelled.discard(job_id)
            for path in acquired:
                release_playback(path)
            shutil.rmtree(job.work_dir, ignore_errors=True)

    async def _render_camera(self, job: "_ExportJob", camera: dict[str, Any], redaction_regions: dict[str, Any], metadata: dict[str, Any], camera_count: int) -> None:
        """One camera's part of the package: its entry in the manifest and, when individual videos are asked for, its video (dewarped, redacted, with audio)."""
        job_id = job.id
        output_modes = job.output_modes
        display_settings = job.display_settings
        include_audio = job.include_audio
        redaction_review_id = job.redaction_review_id
        dewarp_plan = job.dewarp_plan
        start = job.start
        end = job.end
        work_dir = job.work_dir
        if job_id in self._cancelled:
            raise ExportCancelled()
        spans = camera["spans"]
        actual, missing = _interval_metadata(start, end, [
            {"starts_at": span["source_start"].isoformat(), "ends_at": span["source_end"].isoformat()}
            for span in spans
        ])
        if not spans:
            metadata["omitted_cameras"].append({"camera_id": camera["camera_id"], "camera_name": camera["name"], "reason": "No indexed footage overlaps the requested interval"})
            return
        entry: dict[str, Any] = {
            "camera_id": camera["camera_id"], "camera_name": camera["name"],
            "privacy_masked": bool(camera["privacy_zones"]),
            "actual_intervals": actual, "missing_intervals": missing,
            "segment_count": len(spans), "video_file": None,
            "source_to_output_intervals": [
                {"source_start": span["source_start"].isoformat(), "source_end": span["source_end"].isoformat(),
                 "output_start_seconds": round(span["output_start"], 6), "output_end_seconds": round(span["output_end"], 6)}
                for span in spans
            ],
        }
        camera_audio_enabled = bool(include_audio and camera["audio_supported"] and camera["audio_enabled"])
        entry["audio"] = {"included": camera_audio_enabled,
                          "reason": None if camera_audio_enabled else "Audio not requested" if not include_audio else "Camera audio is not administrator-enabled"}
        if "individual" in output_modes:
            manifest = work_dir / f"camera-{camera['index']}.ffconcat"
            video_file = _camera_video_filename(camera["name"], camera["camera_id"], camera["index"])
            clip_path = work_dir / video_file
            # A redacted clip is first rendered under another name and then re-encoded blurred; only the blurred one is ever packaged.
            render_path = work_dir / f"unredacted-{video_file}" if redaction_review_id else clip_path
            subtitle_path = work_dir / f"camera-{camera['index']}.srt"
            lines = ["ffconcat version 1.0\n"]
            for span in spans:
                segment = span["segment"]
                segment_start = _parse_time(segment["starts_at"])
                lines.append(_ffconcat_quote(recording_file_path(segment["relative_path"])))
                lines.append(f"inpoint {(span['source_start'] - segment_start).total_seconds():.6f}\n")
                lines.append(f"outpoint {(span['source_end'] - segment_start).total_seconds():.6f}\n")
            manifest.write_text("".join(lines), encoding="utf-8")
            first_overlay_samples = _write_source_time_subtitles(subtitle_path, camera["name"], spans, display_settings)
            chain = privacy.with_mask(f"fps=15,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,{_subtitle_filter_path(subtitle_path, 1280, 720)}", camera["privacy_zones"])
            camera_dewarp = dewarp_plan.get(camera["camera_id"])
            if camera_dewarp:
                # The mask covers the raw picture first; then the fisheye circle is dewarped to the view the person chose, with the geometry Live uses.
                first_file = recording_file_path(spans[0]["segment"]["relative_path"])
                source_width, source_height = await asyncio.to_thread(_video_size, first_file)
                graph, output_size = await asyncio.to_thread(_dewarp_graph, work_dir, camera["index"], camera_dewarp, source_width, source_height)
                chain = privacy.with_mask(f"fps=15,scale={source_width}:{source_height},{graph},{_subtitle_filter_path(subtitle_path, output_size[0], output_size[1])}", camera["privacy_zones"])
                entry["dewarp"] = {**camera_dewarp["view"], "lens": camera_dewarp["lens"], "output_size": list(output_size), "source_size": [source_width, source_height]}
            await self._render(job_id, lambda encoder, hardware: [
                "-hide_banner", "-loglevel", "error", *encoder.pre, *(acceleration.decode_args() if hardware else []), "-f", "concat", "-safe", "0", "-i", str(manifest),
                "-map", "0:v:0", "-an", "-vf", encoder.filters(chain),
                *encoder.codec, "-movflags", "+faststart", "-y", str(render_path),
            ])
            if job_id in self._cancelled:
                raise ExportCancelled()
            if not render_path.is_file() or render_path.stat().st_size == 0:
                raise RuntimeError(f"No playable video was produced for camera {camera['name']}")
            if redaction_review_id:
                try:
                    counts = await asyncio.to_thread(redaction.redact_clip, render_path, clip_path, redaction_regions, camera["camera_id"], lambda: job_id in self._cancelled)
                except redaction.Cancelled as exc:
                    raise ExportCancelled() from exc
                finally:
                    render_path.unlink(missing_ok=True)
                entry["redaction"] = redaction.camera_summary(redaction_regions, camera["camera_id"], counts)
            if camera_audio_enabled:
                audio_path = work_dir / f"camera-{camera['index']}.m4a"
                duration = sum((span["source_end"] - span["source_start"]).total_seconds() for span in spans)
                await _render_audio(self, job_id, spans, duration, audio_path)
                await _mux_audio(self, job_id, clip_path, audio_path)
                audio_path.unlink(missing_ok=True)
                entry["audio_source_to_output_intervals"] = [
                    {"source_start": span["source_start"].isoformat(), "source_end": span["source_end"].isoformat(),
                     "output_start_seconds": round(span["output_start"], 6), "output_end_seconds": round(span["output_end"], 6)}
                    for span in spans
                ]
            entry["overlay"] = {"time_zone": display_settings["time_zone"], "date_format": display_settings["date_format"],
                                 "clock_format": display_settings["clock_format"], "first_source_labels": first_overlay_samples,
                                 "gap_indicator": "Recording gap skipped"}
            entry["video_file"] = video_file
        metadata["cameras"].append(entry)
        await asyncio.to_thread(self._progress, job_id, 15 + round(55 * (camera["index"] + 1) / max(1, camera_count)))

    async def _render_grid_output(self, job: "_ExportJob", per_camera: list[dict[str, Any]], union: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
        """The synchronized grid video (on the graphics chip first when there is one, in software if that fails), its audio and its overlay manifest."""
        job_id = job.id
        display_settings = job.display_settings
        tile_camera_ids = job.tile_camera_ids
        grid_resolution = job.grid_resolution
        include_audio = job.include_audio
        audio_camera_id = job.audio_camera_id
        work_dir = job.work_dir
        hardware_failure = None
        for hardware in (True, False) if acceleration.selected() else (False,):
            try:
                await _render_grid(self, job_id, work_dir, per_camera, tile_camera_ids, union, grid_resolution, display_settings, hardware)
            except RuntimeError as exc:
                if not hardware or job_id in self._cancelled:
                    raise
                hardware_failure = str(exc)
                continue
            if hardware_failure:
                acceleration.disable(hardware_failure)
            break
        if include_audio:
            source_camera = next(camera for camera in per_camera if camera["camera_id"] == audio_camera_id)
            audio_path = work_dir / "grid.m4a"
            duration = float(metadata["grid"]["duration_seconds"])
            await _render_audio(self, job_id, source_camera["spans"], duration, audio_path, union)
            await _mux_audio(self, job_id, work_dir / "grid.mp4", audio_path)
            audio_path.unlink(missing_ok=True)
            metadata["audio"]["grid_source_to_output_intervals"] = [
                {"source_start": span["source_start"].isoformat(), "source_end": span["source_end"].isoformat(),
                 "output_start_seconds": round(_union_output_time(span["source_start"], union), 6),
                 "output_end_seconds": round(_union_output_time(span["source_end"], union), 6)}
                for span in source_camera["spans"]
            ]
        metadata["grid"]["overlays"] = {
            camera["camera_id"]: {"camera_name": camera["name"], **camera.get("grid_overlay", {}),
                "missing_tile_label": "No video available", "missing_tile_label_alignment": "center",
                "missing_tile_time_labels": ["Shared source time", "Last frame captured"],
                "missing_tile_state": "frozen_last_frame_or_dark_before_first_frame"}
            for camera in per_camera if camera["camera_id"] in tile_camera_ids
        }

    def _write_package(self, job: "_ExportJob", metadata: dict[str, Any], chain_path: Path) -> None:
        """metadata.json and integrity.json, then the zip, checked against the space and size limits and moved into place."""
        job_id = job.id
        output_modes = job.output_modes
        work_dir = job.work_dir
        final_path = job.final_path
        partial_path = job.partial_path
        metadata_path = work_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        package_files = [metadata_path, chain_path] + [work_dir / item["video_file"] for item in metadata["cameras"] if item["video_file"]]
        if "grid" in output_modes:
            package_files.append(work_dir / "grid.mp4")
        integrity = {
            "schema_version": 1,
            "algorithm": "sha256",
            "files": [
                {"path": path.name, "size_bytes": path.stat().st_size, "sha256": footage_chain.file_sha256(path)}
                for path in package_files
            ],
        }
        integrity_path = work_dir / "integrity.json"
        integrity_path.write_text(json.dumps(integrity, indent=2, sort_keys=True), encoding="utf-8")
        estimated_package_bytes = sum(path.stat().st_size for path in package_files) + integrity_path.stat().st_size + 1024 * 1024
        _ensure_export_space(final_path.parent, estimated_package_bytes)
        if job_id in self._cancelled:
            raise ExportCancelled()
        final_path.parent.mkdir(parents=True, exist_ok=True)
        self._progress(job_id, 85)
        archive_files = [(metadata_path, "metadata.json"), (chain_path, "chain.json"), (integrity_path, "integrity.json")]
        archive_files.extend((work_dir / item["video_file"], item["video_file"]) for item in metadata["cameras"] if item["video_file"])
        if "grid" in output_modes:
            archive_files.append((work_dir / "grid.mp4", "grid.mp4"))
        total_archive_bytes = sum(path.stat().st_size for path, _ in archive_files)
        written_archive_bytes = 0
        last_archive_progress = 85
        with zipfile.ZipFile(partial_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for source_path, archive_name in archive_files:
                with source_path.open("rb") as source, archive.open(archive_name, "w") as destination:
                    while chunk := source.read(1024 * 1024):
                        if job_id in self._cancelled:
                            raise ExportCancelled()
                        destination.write(chunk)
                        written_archive_bytes += len(chunk)
                        progress = 85 + round(14 * written_archive_bytes / max(1, total_archive_bytes))
                        if progress > last_archive_progress:
                            self._progress(job_id, progress)
                            last_archive_progress = progress
        if partial_path.stat().st_size > MAX_EXPORT_PACKAGE_BYTES:
            raise RuntimeError("Evidence package exceeds the 2 GiB export limit")
        _ensure_export_space(final_path.parent, partial_path.stat().st_size)
        if job_id in self._cancelled:
            raise ExportCancelled()
        os.replace(partial_path, final_path)

    async def _render(self, job_id: str, build: "Callable[[acceleration.Encoder, bool], list[str]]") -> None:
        """Run an FFmpeg render built for an encoder: on the graphics chip when there is one, and again in software if that fails."""
        hardware_failure = None
        for hardware in (True, False) if acceleration.selected() else (False,):
            encoder = acceleration.encoder(crf=26, speed="ultrafast") if hardware else acceleration.build(None, crf=26, speed="ultrafast")
            try:
                await self._run_ffmpeg(job_id, build(encoder, hardware))
            except RuntimeError as exc:
                if not hardware or job_id in self._cancelled:
                    raise
                hardware_failure = str(exc)  # Maybe the device did not work: redo it in software
                continue
            if hardware_failure:
                acceleration.disable(hardware_failure)  # Software worked where the device did not
            return

    async def _run_ffmpeg(self, job_id: str, args: list[str]) -> None:
        executable = shutil.which("ffmpeg")
        if not executable:
            raise RuntimeError("FFmpeg is unavailable; export cannot run")
        output_path = Path(args[-1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(output_path.parent).free
        copies_budget.make_room(0)  # Over the budget already: playback copies go first, and then this export is refused (#362)
        output_limit = min(MAX_EXPORT_PACKAGE_BYTES, free - free_floor(output_path.parent), copies_budget.room_left())
        if output_limit <= 0:
            raise RuntimeError("Insufficient free storage for evidence export: the recordings volume's headroom and the export budget must not be exceeded")
        args = list(args)
        args[-1:-1] = ["-fs", str(output_limit)]
        args[3:3] = ["-progress", "pipe:1", "-nostats"]
        process = await asyncio.create_subprocess_exec(
            executable, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self._processes[job_id] = process
        # Cancellation can arrive while create_subprocess_exec is yielding,
        # before this process is visible to ExportManager.cancel(). Recheck
        # immediately after registration so that job cannot run uncancellable.
        if job_id in self._cancelled and process.returncode is None:
            process.terminate()
        async def read_progress() -> None:
            assert process.stdout is not None
            while line := await process.stdout.readline():
                if line.startswith(b"out_time_us="):
                    try:
                        elapsed = int(line.split(b"=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    try:
                        await asyncio.to_thread(self._advance_progress, job_id, elapsed > 0)
                    except sqlite3.Error:
                        pass  # The figure is for people; FFmpeg's output must keep being read or it stops (#357)

        try:
            progress_task = asyncio.create_task(read_progress())
            stderr_task = asyncio.create_task(process.stderr.read())
            await asyncio.wait_for(process.wait(), timeout=60 * 60)
            await progress_task
            stderr = await stderr_task
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RuntimeError("Export encoding exceeded the one-hour job limit") from exc
        except asyncio.CancelledError:
            await end_process(process, 3)
            raise
        finally:
            self._processes.pop(job_id, None)
        if job_id in self._cancelled:
            raise ExportCancelled()
        if process.returncode != 0:
            reason = stderr.decode("utf-8", errors="replace")[-400:].strip()
            raise RuntimeError(reason or "FFmpeg could not create a playable export")
        if output_path.is_file() and output_path.stat().st_size >= output_limit:
            raise RuntimeError("Evidence output reached its bounded size limit")


def _video_size(path: Path) -> tuple[int, int]:
    """The picture size of a recorded segment."""
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is unavailable; the recorded picture's size cannot be read for dewarping")
    try:
        result = subprocess.run([executable, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                                capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("The recorded picture's size could not be read for dewarping in time") from exc
    try:
        width, height = (int(value) for value in result.stdout.strip().split(",")[:2])
    except ValueError as exc:
        raise RuntimeError("The recorded picture's size could not be read for dewarping") from exc
    return width, height


def _dewarp_graph(work_dir: Path, index: int, plan: dict[str, Any], source_width: int, source_height: int) -> tuple[str, tuple[int, int]]:
    """Write the pixel maps for a camera's dewarped view and return the filter graph that applies them, and the size of the picture it makes."""
    lens = dewarp_geometry.Lens(**plan["lens"])
    view = dewarp_geometry.View(**plan["view"])
    xmap, ymap, size = dewarp_geometry.remap_maps(lens, view, source_width, source_height)
    x_path, y_path = work_dir / f"dewarp-{index}-x.pgm", work_dir / f"dewarp-{index}-y.pgm"
    dewarp_geometry.write_pgm(x_path, xmap)
    dewarp_geometry.write_pgm(y_path, ymap)
    return dewarp_geometry.graph(x_path, y_path, size), size


def _checked_request(camera_ids: list[str], output_modes: list[str] | None, tile_camera_ids: list[str | None] | None, grid_resolution: str,
                     include_audio: bool, audio_camera_id: str | None) -> tuple[list[str], list[str | None]]:
    """The checks that need no database: the outputs, the grid and its tiles, and the audio choice. Returns the outputs and the tile layout to use."""
    output_modes = output_modes or ["individual", "grid"]
    if not output_modes or len(set(output_modes)) != len(output_modes) or any(mode not in {"individual", "grid"} for mode in output_modes):
        raise ValueError("Choose individual, grid, or both export outputs")
    if grid_resolution not in {"1080p", "4k"}:
        raise ValueError("Choose 1080p or 4K grid resolution")
    if include_audio and "grid" in output_modes and not audio_camera_id:
        raise ValueError("Choose one selected camera as the grid audio source")
    if include_audio and audio_camera_id is not None and ("grid" not in output_modes or audio_camera_id not in camera_ids):
        raise ValueError("Grid audio source must be one selected camera")
    if not include_audio and audio_camera_id is not None:
        raise ValueError("Grid audio source requires audio to be enabled")
    if not tile_camera_ids:
        size = next(size for size in (1, 4, 9, 16) if size >= len(camera_ids))
        tile_camera_ids = list(camera_ids) + [None] * (size - len(camera_ids))
    tile_ids = [item for item in tile_camera_ids if item is not None]
    if len(tile_camera_ids) not in {1, 4, 9, 16} or len(tile_ids) != len(set(tile_ids)) or set(tile_ids) != set(camera_ids):
        raise ValueError("Grid tiles must include each selected camera exactly once")
    return output_modes, tile_camera_ids


def _check_cameras_and_audio(db: sqlite3.Connection, camera_ids: list[str], output_modes: list[str], include_audio: bool, audio_camera_id: str | None) -> None:
    """Every camera exists, and the audio asked for comes from a camera an administrator enabled it on."""
    placeholders = ",".join("?" for _ in camera_ids)
    present = {row[0] for row in db.execute(f"SELECT id FROM cameras WHERE id IN ({placeholders})", tuple(camera_ids))}
    if present != set(camera_ids):
        raise LookupError("Export request contains an unknown camera")
    if include_audio:
        capability_rows = db.execute(f"SELECT id,audio_supported,audio_enabled FROM cameras WHERE id IN ({placeholders})", tuple(camera_ids)).fetchall()
        if "individual" in output_modes and "grid" not in output_modes and not any(row["audio_supported"] == 1 and row["audio_enabled"] == 1 for row in capability_rows):
            raise ValueError("Individual audio requires at least one administrator-enabled camera audio source")
        if "grid" in output_modes:
            source = next((row for row in capability_rows if row["id"] == audio_camera_id), None)
            if source is None or source["audio_supported"] != 1 or source["audio_enabled"] != 1:
                raise ValueError("Grid audio source must have administrator-enabled audio")


def _export_segments(db: sqlite3.Connection, camera_ids: list[str], start: datetime, end: datetime) -> list[sqlite3.Row]:
    """The indexed footage the export will be cut from; refused when there is none or when it is more than an export may take."""
    placeholders = ",".join("?" for _ in camera_ids)
    segments = db.execute(
        f"SELECT id,size_bytes FROM recording_segments WHERE camera_id IN ({placeholders}) AND starts_at<? AND ends_at>? AND unavailable_at IS NULL",
        (*camera_ids, end.isoformat(), start.isoformat()),
    ).fetchall()
    if not segments:
        raise LookupError("No indexed footage is available in the requested interval")
    if len(segments) > MAX_EXPORT_SEGMENTS or sum(int(segment["size_bytes"]) for segment in segments) > MAX_EXPORT_SOURCE_BYTES:
        raise OverflowError("Export source exceeds the supported size or segment limit")
    too_many = _too_many_files(len(segments))
    if too_many:
        raise ValueError(too_many)  # Refused up front, in plain words, rather than failing part-way (#356)
    return segments


def _dewarp_plan(db: sqlite3.Connection, dewarp: dict[str, dict[str, Any]] | None, dewarp_lenses: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    """The lens and view of each dewarped camera, checked: a retry renders with the lens of the original request, as its manifest says."""
    plan: dict[str, Any] = {}
    for camera_id, view in (dewarp or {}).items():
        kept = (dewarp_lenses or {}).get(camera_id)
        lens = ({"mount": kept["mount"], "center_x": kept["cx"], "center_y": kept["cy"], "radius": kept["radius"], "fov": kept["fov"]} if kept
                else db.execute("SELECT * FROM camera_lens WHERE camera_id=?", (camera_id,)).fetchone())
        if lens is None:
            raise ValueError("A dewarped view needs a fisheye camera: set the camera's lens first")
        if view.get("mode") not in dewarp_geometry.MODES:
            raise ValueError("Choose a view, a panorama or a double panorama")
        if lens["mount"] == "wall" and view["mode"] == "double_panorama":
            raise ValueError("A wall-mounted camera sees 180 degrees: use a panorama")
        plan[camera_id] = {"lens": {"mount": lens["mount"], "cx": lens["center_x"], "cy": lens["center_y"], "radius": lens["radius"], "fov": lens["fov"]},
                           "view": {"mode": view["mode"], "pan": float(view.get("pan", 0)), "tilt": float(view.get("tilt", -90)), "fov": float(view.get("fov", 90))}}
    return plan


def create_export_record(
    requested_by: int, camera_ids: list[str], start: datetime, end: datetime,
    output_modes: list[str] | None = None,
    tile_camera_ids: list[str | None] | None = None, grid_resolution: str = "1080p",
    include_audio: bool = False, audio_camera_id: str | None = None, retry_of: str | None = None,
    redaction_review_id: str | None = None, dewarp: dict[str, dict[str, Any]] | None = None, dewarp_lenses: dict[str, dict[str, Any]] | None = None,
) -> str:
    output_modes, tile_camera_ids = _checked_request(camera_ids, output_modes, tile_camera_ids, grid_resolution, include_audio, audio_camera_id)
    # Refused up front, with the reason, when exports and case packages already fill their share of the disk (#362).
    copies_budget.make_room(MIN_EXPORT_FREE_BYTES)
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute("SELECT COUNT(*) FROM exports WHERE status IN ('queued','running')").fetchone()[0]
        if active >= MAX_ACTIVE_EXPORTS:
            raise OverflowError("The export queue is full; wait for an existing job to finish")
        _check_cameras_and_audio(db, camera_ids, output_modes, include_audio, audio_camera_id)
        segments = _export_segments(db, camera_ids, start, end)
        dewarp_plan = _dewarp_plan(db, dewarp, dewarp_lenses)
        display_row = db.execute("SELECT time_zone,date_format,clock_format FROM site_display_settings WHERE singleton_id=1").fetchone()
        display_settings = {"time_zone": display_row["time_zone"], "date_format": display_row["date_format"], "clock_format": display_row["clock_format"]}
        db.execute("INSERT INTO exports(id,requested_by,camera_ids_json,output_modes_json,display_settings_json,tile_camera_ids_json,grid_resolution,requested_start,requested_end,status,include_audio,audio_camera_id,created_at,updated_at,retry_of) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?)",
                   (job_id, requested_by, json.dumps(camera_ids), json.dumps(output_modes), json.dumps(display_settings), json.dumps(tile_camera_ids), grid_resolution, start.isoformat(), end.isoformat(), int(include_audio), audio_camera_id, now, now, retry_of))
        db.executemany("INSERT INTO export_sources(export_id,segment_id) VALUES(?,?)", ((job_id, segment["id"]) for segment in segments))
        if dewarp_plan:
            db.execute("UPDATE exports SET dewarp_json=? WHERE id=?", (json.dumps(dewarp_plan, sort_keys=True), job_id))
        if redaction_review_id:
            redaction.claim_for_export(db, redaction_review_id, requested_by, job_id, camera_ids, start, end, output_modes, retry_of)
            db.execute("UPDATE exports SET redaction_review_id=? WHERE id=?", (redaction_review_id, job_id))
        viewing_audit.record_events(db, requested_by, camera_ids, "export", job_id)
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (requested_by, "export_retried" if retry_of else "export_requested", "export", job_id, json.dumps({
                       "cameras": len(camera_ids), "start": start.isoformat(), "end": end.isoformat(), "output_modes": output_modes,
                       "include_audio": include_audio, "audio_camera_id": audio_camera_id, **({"retry_of": retry_of} if retry_of else {}),
                   })))
    return job_id


def get_export(job_id: str, requester_id: int, is_admin: bool = False) -> sqlite3.Row | None:
    expire_exports()
    with connect() as db:
        if is_admin:
            return db.execute("SELECT * FROM exports WHERE id=?", (job_id,)).fetchone()
        return db.execute("SELECT * FROM exports WHERE id=? AND requested_by=?", (job_id, requester_id)).fetchone()


def list_exports(requester_id: int, is_admin: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    expire_exports()
    with connect() as db:
        if is_admin:
            rows = db.execute("SELECT * FROM exports ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM exports WHERE requested_by=? ORDER BY created_at DESC LIMIT ?", (requester_id, limit)).fetchall()
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
