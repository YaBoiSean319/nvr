from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import footage_chain, privacy
from .display_settings import format_display_datetime
from .recording import recordings_path
from .retention import acquire_playback, release_playback, unlink_if_unleased
from .storage import recording_file_path
from .store import connect

MAX_EXPORT_WORKERS = 1
POLL_SECONDS = 2
MAX_ACTIVE_EXPORTS = 4
MAX_EXPORT_CAMERAS = 16
MAX_EXPORT_WINDOW = timedelta(hours=24)
MAX_EXPORT_SOURCE_BYTES = 100 * 1024**3
MAX_EXPORT_SEGMENTS = 5000
EXPORT_DIRECTORY = "exports"
MAX_VERIFY_PACKAGE_BYTES = 2 * 1024**3
MAX_EXPORT_PACKAGE_BYTES = 2 * 1024**3
MIN_EXPORT_FREE_BYTES = 256 * 1024**2
EXPORT_RETENTION = timedelta(days=7)


def verify_chain_member(archive: zipfile.ZipFile) -> dict[str, Any]:
    """Check the package's signed chain extract offline, with the public key it carries."""
    package = json.loads(archive.read("chain.json"))
    cameras = []
    for camera in package.get("cameras", []):
        if not camera.get("chain"):
            cameras.append({"camera_id": camera.get("camera_id"), "status": "not_signed", "error": None})
            continue
        result = footage_chain.verify_extract(camera["chain"], camera.get("sources", []))
        cameras.append({"camera_id": camera.get("camera_id"), "status": "valid" if result["valid"] else "invalid", "error": result["error"],
                        "public_key": result["public_key"]})
    return {"valid": all(item["status"] != "invalid" for item in cameras), "cameras": cameras}


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
            chain = verify_chain_member(archive) if "chain.json" in delivered else None
            return {"checksums_valid": valid, "files": files, "chain": chain,
                    "error": None if valid else "one or more file checksums do not match"}
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"checksums_valid": False, "files": [], "error": f"invalid evidence package: {str(exc)[:200]}"}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    if free - requested_bytes < MIN_EXPORT_FREE_BYTES:
        raise RuntimeError("Insufficient free storage for evidence export; at least 256 MiB must remain")


def _has_audio(path: Path) -> bool:
    probe = shutil.which("ffprobe")
    if not probe:
        raise RuntimeError("ffprobe is unavailable; optional audio cannot be verified")
    result = subprocess.run([probe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=15, check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


async def _render_audio(manager: "ExportManager", job_id: str, work_dir: Path, spans: list[dict[str, Any]],
                        duration: float, output: Path, grid_intervals: list[dict[str, Any]] | None = None) -> None:
    """Render a selected source's audio on output time, leaving missing spans silent."""
    if duration <= 0:
        raise RuntimeError("Audio export has no recorded duration")
    args = ["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:duration={duration:.6f}"]
    graph = [f"[0:a]atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[silence]"]
    labels = ["[silence]"]
    input_index = 1
    audio_presence: dict[Path, bool] = {}
    for span in spans:
        segment = span["segment"]
        source_path = recording_file_path(segment["relative_path"])
        if source_path not in audio_presence:
            audio_presence[source_path] = await asyncio.to_thread(_has_audio, source_path)
        if not audio_presence[source_path]:
            continue
        source_offset = max(0.0, (span["source_start"] - _parse_time(segment["starts_at"])).total_seconds())
        span_duration = (span["source_end"] - span["source_start"]).total_seconds()
        out_start = _union_output_time(span["source_start"], grid_intervals) if grid_intervals is not None else span["output_start"]
        delay_ms = max(0, round(out_start * 1000))
        args.extend(["-ss", f"{source_offset:.6f}", "-t", f"{span_duration:.6f}", "-i", str(source_path)])
        label = f"aud{input_index}"
        graph.append(f"[{input_index}:a:0]aresample=48000,atrim=duration={span_duration:.6f},asetpts=PTS-STARTPTS,adelay={delay_ms}|{delay_ms}[{label}]")
        labels.append(f"[{label}]")
        input_index += 1
    graph.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:normalize=0,atrim=duration={duration:.6f}[out]")
    args.extend(["-filter_complex", ";".join(graph), "-map", "[out]", "-c:a", "aac", "-b:a", "128k", "-y", str(output)])
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


async def _render_grid(manager: "ExportManager", job_id: str, work_dir: Path, cameras: list[dict[str, Any]],
                       tile_camera_ids: list[str | None], intervals: list[dict[str, Any]], resolution: str,
                       display_settings: dict[str, str]) -> Path:
    width, height = (1920, 1080) if resolution == "1080p" else (3840, 2160)
    columns = int(len(tile_camera_ids) ** 0.5)
    tile_width, tile_height = width // columns, height // columns
    duration = sum((item["source_end"] - item["source_start"]).total_seconds() for item in intervals if not item["skipped"])
    by_id = {camera["camera_id"]: camera for camera in cameras}
    args = ["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=black:s={tile_width}x{tile_height}:r=15:d={duration:.6f}"]
    graph: list[str] = []
    tile_outputs: list[str] = []
    next_input = 1
    for tile_index, camera_id in enumerate(tile_camera_ids):
        graph.append(f"[0:v]null[tile{tile_index}_base]" if tile_index == 0 else f"color=c=black:s={tile_width}x{tile_height}:r=15:d={duration:.6f}[tile{tile_index}_base]")
        current = f"tile{tile_index}_base"
        camera = by_id.get(camera_id) if camera_id else None
        spans = camera["spans"] if camera else []
        subtitle_path = work_dir / f"grid-tile-{tile_index}.srt"
        overlay_mapping = _write_grid_subtitles(subtitle_path, camera["name"] if camera else "Selected camera", spans, intervals, duration, display_settings)
        if camera:
            camera["grid_overlay"] = overlay_mapping
        offsets = [(_union_output_time(span["source_start"], intervals), _union_output_time(span["source_end"], intervals)) for span in spans]
        for span_index, span in enumerate(spans):
            segment = span["segment"]
            source_offset = max(0.0, (span["source_start"] - _parse_time(segment["starts_at"])).total_seconds())
            span_duration = (span["source_end"] - span["source_start"]).total_seconds()
            output_start = offsets[span_index][0]
            next_start = offsets[span_index + 1][0] if span_index + 1 < len(spans) else duration
            args.extend(["-ss", f"{source_offset:.6f}", "-t", f"{span_duration:.6f}", "-i", str(recording_file_path(segment["relative_path"]))])
            clip, composed = f"tile{tile_index}_clip{span_index}", f"tile{tile_index}_composed{span_index}"
            mask = f"{privacy.drawbox_filter(camera['privacy_zones'])}," if camera.get("privacy_zones") else ""
            graph.append(f"[{next_input}:v]{mask}fps=15,scale={tile_width}:{tile_height}:force_original_aspect_ratio=decrease,pad={tile_width}:{tile_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,tpad=stop_mode=clone:stop_duration={max(0.01, next_start-output_start-span_duration):.6f},trim=duration={max(0.01, next_start-output_start):.6f},setpts=PTS-STARTPTS+{output_start:.6f}/TB[{clip}]")
            graph.append(f"[{current}][{clip}]overlay=eof_action=pass:shortest=0:enable='between(t,{output_start:.6f},{next_start:.6f})'[{composed}]")
            current = composed
            next_input += 1
        subbed = f"tile{tile_index}_subbed"
        graph.append(f"[{current}]{_subtitle_filter_path(subtitle_path, tile_width, tile_height, OVERLAY_MIN_TILE_TEXT_PX)}[{subbed}]")
        tile_outputs.append(f"[{subbed}]")
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
    graph.append(f"{final_filter},{_subtitle_filter_path(gaps, width, height)}[out]")
    output = work_dir / "grid.mp4"
    args.extend(["-filter_complex", ";".join(graph), "-map", "[out]", "-an", "-r", "15", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-movflags", "+faststart", "-t", f"{duration:.6f}", "-y", str(output)])
    await manager._run_ffmpeg(job_id, args)
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
    }


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
        self._pending: set[str] = set()
        self._running: str | None = None

    async def start(self) -> None:
        recover_interrupted_exports()
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

    async def _expire_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            expire_exports()

    async def _poll_loop(self) -> None:
        """Pick up queued jobs and cancel requests even when a nudge was lost."""
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                with connect() as db:
                    queued = [row[0] for row in db.execute("SELECT id FROM exports WHERE status='queued' ORDER BY created_at")]
                    cancelling = {row[0] for row in db.execute("SELECT id FROM exports WHERE status='running' AND cancel_requested=1")}
            except sqlite3.Error:
                continue
            for job_id in queued:
                self.enqueue(job_id)
            if self._running in cancelling and self._running not in self._cancelled:
                await self.cancel(self._running)

    async def stop(self) -> None:
        self._stopping = True
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
            finally:
                self._running = None
                self._queue.task_done()

    async def _execute(self, job_id: str) -> None:
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
            final_path.parent.mkdir(parents=True, exist_ok=True)
            _ensure_export_space(final_path.parent)
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
                    segments = [dict(item) for item in db.execute(
                        "SELECT id,relative_path,starts_at,ends_at,size_bytes FROM recording_segments "
                        "WHERE camera_id=? AND starts_at<? AND ends_at>? AND unavailable_at IS NULL ORDER BY starts_at",
                        (camera_id, end.isoformat(), start.isoformat()),
                    ).fetchall()]
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
            self._progress(job_id, 10)
            union = _union_intervals(per_camera, start, end) if "grid" in output_modes else []
            if union:
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
                metadata["grid"] = {"video_file": "grid.mp4", "resolution": grid_resolution, "tile_camera_ids": tile_camera_ids,
                                     "duration_seconds": round(compressed, 6), "source_to_output_intervals": source_to_output,
                                     "global_gaps_compressed": global_gaps}
            for camera in per_camera:
                if job_id in self._cancelled:
                    raise ExportCancelled()
                camera_segments = camera["segments"]
                spans = camera["spans"]
                actual, missing = _interval_metadata(start, end, [
                    {"starts_at": span["source_start"].isoformat(), "ends_at": span["source_end"].isoformat()}
                    for span in spans
                ])
                if not spans:
                    metadata["omitted_cameras"].append({"camera_id": camera["camera_id"], "camera_name": camera["name"], "reason": "No indexed footage overlaps the requested interval"})
                    continue
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
                entry["video_file"] = None
                if "individual" in output_modes:
                    manifest = work_dir / f"camera-{camera['index']}.ffconcat"
                    video_file = _camera_video_filename(camera["name"], camera["camera_id"], camera["index"])
                    clip_path = work_dir / video_file
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
                    await self._run_ffmpeg(job_id, [
                        "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(manifest),
                        "-map", "0:v:0", "-an", "-vf", privacy.with_mask(f"fps=15,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,{_subtitle_filter_path(subtitle_path, 1280, 720)}", camera["privacy_zones"]),
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-movflags", "+faststart", "-y", str(clip_path),
                    ])
                    if job_id in self._cancelled:
                        raise ExportCancelled()
                    if not clip_path.is_file() or clip_path.stat().st_size == 0:
                        raise RuntimeError(f"No playable video was produced for camera {camera['name']}")
                    if camera_audio_enabled:
                        audio_path = work_dir / f"camera-{camera['index']}.m4a"
                        duration = sum((span["source_end"] - span["source_start"]).total_seconds() for span in spans)
                        await _render_audio(self, job_id, work_dir, spans, duration, audio_path)
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
                self._progress(job_id, 15 + round(55 * (camera["index"] + 1) / max(1, len(per_camera))))

            if "grid" in output_modes:
                await _render_grid(self, job_id, work_dir, per_camera, tile_camera_ids, union, grid_resolution, display_settings)
                if include_audio:
                    source_camera = next(camera for camera in per_camera if camera["camera_id"] == audio_camera_id)
                    audio_path = work_dir / "grid.m4a"
                    duration = float(metadata["grid"]["duration_seconds"])
                    await _render_audio(self, job_id, work_dir, source_camera["spans"], duration, audio_path, union)
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

            work_dir.mkdir(parents=True, exist_ok=True)
            # Signed footage: each source segment's hash now, and the signed chain covering it, for offline checks.
            chain_package: dict[str, Any] = {"schema_version": 1, "algorithm": "Ed25519", "cameras": []}
            with connect() as db:
                for camera in per_camera:
                    sources = [{"segment_id": int(segment["id"]), "relative_path": segment["relative_path"],
                                "sha256": await asyncio.to_thread(footage_chain.file_sha256, recording_file_path(segment["relative_path"]))}
                               for segment in camera["segments"]]
                    extract = footage_chain.extract(db, camera["camera_id"], [source["segment_id"] for source in sources])
                    chain_package["cameras"].append({"camera_id": camera["camera_id"], "sources": sources, "chain": extract})
            chain_path = work_dir / "chain.json"
            chain_path.write_text(json.dumps(chain_package, indent=2, sort_keys=True), encoding="utf-8")
            metadata["signed_footage"] = {camera["camera_id"]: ("signed" if camera["chain"] else "not_signed") for camera in chain_package["cameras"]}
            expires_at = (datetime.now(timezone.utc) + EXPORT_RETENTION).isoformat()
            metadata["expires_at"] = expires_at
            metadata_path = work_dir / "metadata.json"
            metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
            package_files = [metadata_path, chain_path] + [work_dir / item["video_file"] for item in metadata["cameras"] if item["video_file"]]
            if "grid" in output_modes:
                package_files.append(work_dir / "grid.mp4")
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
            completed_at = datetime.now(timezone.utc)
            with connect() as db:
                updated = db.execute("UPDATE exports SET status='completed',progress=100,expires_at=?,metadata_json=?,output_relative_path=?,updated_at=?,completed_at=?,error=NULL WHERE id=? AND status='running' AND cancel_requested=0",
                           (expires_at, json.dumps(metadata, separators=(",", ":")), f"{EXPORT_DIRECTORY}/{job_id}.zip", completed_at.isoformat(), completed_at.isoformat(), job_id))
                if updated.rowcount != 1:
                    raise ExportCancelled()
                db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                           (requester, "export_completed", "export", job_id, json.dumps({"camera_count": len(metadata["cameras"]), "omitted_camera_count": len(metadata["omitted_cameras"]), "segment_count": total_segments})))
        except ExportCancelled:
            partial_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            with connect() as db:
                db.execute("UPDATE exports SET status='cancelled',progress=0,cancel_requested=0,error=NULL,updated_at=?,completed_at=? WHERE id=? AND status='running'", (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), job_id))
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
        output_path = Path(args[-1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(output_path.parent).free
        output_limit = min(MAX_EXPORT_PACKAGE_BYTES, free - MIN_EXPORT_FREE_BYTES)
        if output_limit <= 0:
            raise RuntimeError("Insufficient free storage for evidence export; at least 256 MiB must remain")
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
                    with connect() as db:
                        row = db.execute("SELECT progress FROM exports WHERE id=?", (job_id,)).fetchone()
                    current = int(row[0]) if row else 0
                    self._progress(job_id, max(current, min(84, current + (1 if elapsed > 0 else 0))))

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
        if output_path.is_file() and output_path.stat().st_size >= output_limit:
            raise RuntimeError("Evidence output reached its bounded size limit")


def create_export_record(
    requested_by: int, camera_ids: list[str], start: datetime, end: datetime,
    output_modes: list[str] | None = None,
    tile_camera_ids: list[str | None] | None = None, grid_resolution: str = "1080p",
    include_audio: bool = False, audio_camera_id: str | None = None, retry_of: str | None = None,
) -> str:
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
        if include_audio:
            capability_rows = db.execute(f"SELECT id,audio_supported,audio_enabled FROM cameras WHERE id IN ({placeholders})", tuple(camera_ids)).fetchall()
            if "individual" in output_modes and "grid" not in output_modes and not any(row["audio_supported"] == 1 and row["audio_enabled"] == 1 for row in capability_rows):
                raise ValueError("Individual audio requires at least one administrator-enabled camera audio source")
            if "grid" in output_modes:
                source = next((row for row in capability_rows if row["id"] == audio_camera_id), None)
                if source is None or source["audio_supported"] != 1 or source["audio_enabled"] != 1:
                    raise ValueError("Grid audio source must have administrator-enabled audio")
        segments = db.execute(
            f"SELECT id,size_bytes FROM recording_segments WHERE camera_id IN ({placeholders}) AND starts_at<? AND ends_at>? AND unavailable_at IS NULL",
            (*camera_ids, end.isoformat(), start.isoformat()),
        ).fetchall()
        count = len(segments)
        size = sum(int(segment["size_bytes"]) for segment in segments)
        if not count:
            raise LookupError("No indexed footage is available in the requested interval")
        if count > MAX_EXPORT_SEGMENTS or size > MAX_EXPORT_SOURCE_BYTES:
            raise OverflowError("Export source exceeds the supported size or segment limit")
        display_row = db.execute("SELECT time_zone,date_format,clock_format FROM site_display_settings WHERE singleton_id=1").fetchone()
        display_settings = {"time_zone": display_row["time_zone"], "date_format": display_row["date_format"], "clock_format": display_row["clock_format"]}
        db.execute("INSERT INTO exports(id,requested_by,camera_ids_json,output_modes_json,display_settings_json,tile_camera_ids_json,grid_resolution,requested_start,requested_end,status,include_audio,audio_camera_id,created_at,updated_at,retry_of) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?)",
                   (job_id, requested_by, json.dumps(camera_ids), json.dumps(output_modes), json.dumps(display_settings), json.dumps(tile_camera_ids), grid_resolution, start.isoformat(), end.isoformat(), int(include_audio), audio_camera_id, now, now, retry_of))
        db.executemany("INSERT INTO export_sources(export_id,segment_id) VALUES(?,?)", ((job_id, segment["id"]) for segment in segments))
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


def list_exports(requester_id: int, is_admin: bool = False) -> list[dict[str, Any]]:
    expire_exports()
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
