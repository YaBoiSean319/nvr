"""Synchronized playback timeline, segment media, snapshots and frame navigation."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, model_validator

from app import console
from app import ai_coverage, clip_labels, external_events, privacy, track_privacy, viewing_audit
from app.clock import canon
from app.coverage import timeline as coverage_timeline
from app.principal import CurrentUser, requires_scope
from app.processes import run_to_end
from app.store import connect
from app.retention import acquire_playback, release_playback

router = APIRouter()


class SynchronizedPlaybackRequest(BaseModel):
    camera_ids: list[str] = Field(min_length=1, max_length=16)
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_interval(self) -> "SynchronizedPlaybackRequest":
        if len(set(self.camera_ids)) != len(self.camera_ids):
            raise ValueError("Choose each camera only once")
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError("Playback interval must include timezone-aware start and end times")
        if self.end - self.start > timedelta(hours=24):
            raise ValueError("Synchronized playback is limited to 24 hours per search")
        return self


@router.post("/api/playback/timeline")
@requires_scope("read")
@console.federated(console.split_timeline, console.combine_timeline)
def synchronized_playback_timeline(
    payload: SynchronizedPlaybackRequest,
    user: CurrentUser,
) -> dict[str, object]:
    start = payload.start.astimezone(timezone.utc)
    end = payload.end.astimezone(timezone.utc)
    start_text, end_text = start.isoformat(), end.isoformat()
    cameras: list[dict[str, object]] = []
    with connect() as db:
        for camera_id in payload.camera_ids:
            camera = db.execute("SELECT id,name,recording_status,audio_supported,audio_enabled,video_codec FROM cameras WHERE id=?", (camera_id,)).fetchone()
            if camera is None or not user.access.can_see(camera_id):  # A hidden camera is an unknown one.
                raise HTTPException(status_code=422, detail="Playback request contains an unknown camera")
            recorded = coverage_timeline(db, camera_id, start, end, limit=None)  # Every segment of the search: gaps worked out from a cut-off list are false (#360)
            segments = [{**row, "media_url": f"/api/cameras/{camera_id}/segments/{row['id']}/media"} for row in recorded["segments"]]
            coverage, gaps = recorded["coverage"], recorded["gaps"]
            event_rows = db.execute(
                "SELECT id,source_event_at,requested_start,requested_end,actual_start,actual_end,"
                "pre_roll_complete,status,failure_reason,segment_id FROM motion_events "
                "WHERE camera_id=? AND source_event_at>=? AND source_event_at<? ORDER BY source_event_at LIMIT 1000",
                (camera_id, start_text, end_text),
            ).fetchall()
            events = [{
                **dict(row), "pre_roll_complete": bool(row["pre_roll_complete"]),
                "media_url": f"/api/cameras/{camera_id}/segments/{row['segment_id']}/media" if row["segment_id"] else None,
            } for row in event_rows]
            masks = track_privacy.zones_by_camera(db, [camera_id]).get(camera_id, [])  # An object behind a privacy mask is not on the timeline (#296)
            ai_detections = [{key: row[key] for key in ("id", "label", "confidence", "first_seen_at", "last_seen_at")} for row in db.execute(
                "SELECT id,label,confidence,first_seen_at,last_seen_at,box_json FROM ai_observations "
                "WHERE camera_id=? AND first_seen_at>=? AND first_seen_at<? ORDER BY first_seen_at LIMIT 1000",
                (camera_id, start_text, end_text)) if not (masks and track_privacy.box_touches(json.loads(row["box_json"]), masks))]
            ai_unanalyzed = [{key: row[key] for key in ("state", "starts_at", "ends_at", "detail")}
                             for row in ai_coverage.overlapping(db, camera_id, canon(start_text), canon(end_text)) if row["state"] != "analyzed"][:500]
            padded_start, padded_end = (start - timedelta(minutes=1)).isoformat(), (end + timedelta(minutes=1)).isoformat()
            seen, seen_values = track_privacy.track_visible_sql(track_privacy.zones_by_camera(db, [camera_id]), alias="t", window=(padded_start, padded_end))
            clip_labels.label_events(events, [dict(row) for row in db.execute(
                f"SELECT t.label,t.first_seen_at,t.last_seen_at FROM ai_tracks t WHERE t.camera_id=? AND t.first_seen_at<? AND t.last_seen_at>=? AND {seen}",
                (camera_id, padded_end, padded_start, *seen_values))], [{"starts_at": row["starts_at"], "ends_at": row["ends_at"]}
                for row in ai_coverage.overlapping(db, camera_id, canon(padded_start), canon(padded_end)) if row["state"] == "analyzed"])
            building_events = [{"id": row["id"], "type": row["type"], "label": external_events.label(row["type"]), "subject": row["subject"], "message": row["message"],
                                "occurred_at": row["occurred_at"], "source_name": row["source_name"]} for row in db.execute(
                "SELECT e.id,e.type,e.subject,e.message,e.occurred_at,e.source_name FROM external_events e JOIN external_event_cameras k ON k.event_id=e.id "
                "WHERE k.camera_id=? AND e.occurred_at>=? AND e.occurred_at<? ORDER BY e.occurred_at LIMIT 1000", (camera_id, start_text, end_text))]
            cameras.append({"camera_id": camera_id, "name": camera["name"], "recording_status": camera["recording_status"], "video_codec": camera["video_codec"],
                            "audio_supported": None if camera["audio_supported"] is None else bool(camera["audio_supported"]), "audio_enabled": bool(camera["audio_enabled"]),
                            "segments": segments, "coverage": coverage, "motion_events": events, "gaps": gaps,
                            "ai_detections": ai_detections, "ai_unanalyzed": ai_unanalyzed, "external_events": building_events})
    return {"start": start_text, "end": end_text, "cameras": cameras}


def _segment_zones(user, camera_id: str, starts_at: str, unmasked: bool) -> list:
    recorded_at = datetime.fromisoformat(starts_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    try:
        return privacy.zones_for(user, camera_id, unmasked=unmasked, recorded_at=recorded_at)
    except privacy.UnmaskedRefused as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _segment_and_zones(columns: str, segment_id: int, camera_id: str, user, unmasked: bool) -> tuple:
    """A recording's row and the privacy zones this person sees it through. Run in a thread by the asynchronous handlers (#354)."""
    with connect() as db:
        row = db.execute(f"SELECT {columns} FROM recording_segments WHERE id=? AND camera_id=?", (segment_id, camera_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Recorded interval is unavailable")
    return row, _segment_zones(user, camera_id, row["starts_at"], unmasked)


@router.get("/api/cameras/{camera_id}/segments/{segment_id}/media")
@requires_scope("media")
async def play_segment(
    camera_id: str,
    segment_id: int,
    user: CurrentUser,
    unmasked: Annotated[bool, Query()] = False,
    compat: Annotated[Literal["h264"] | None, Query()] = None,
) -> FileResponse:
    """The recording. ``compat=h264`` asks for it converted to H.264, for a browser that cannot play the camera's H.265."""
    row, zones = await asyncio.to_thread(_segment_and_zones, "relative_path,starts_at", segment_id, camera_id, user, unmasked)
    from app.storage import recording_file_path
    try:
        path = await run_in_threadpool(recording_file_path, row["relative_path"])  # Footage on a cold tier may have to be fetched: not on the event loop
    except (OSError, ValueError):
        raise HTTPException(status_code=410, detail="Recorded interval is unavailable") from None
    acquire_playback(path)
    if not path.is_file():
        release_playback(path)
        raise HTTPException(status_code=410, detail="Recorded interval is unavailable") from None
    if zones or compat:
        # Recordings on disk are not masked: deliver a masked rendering, never the file itself. (Or, for ``compat``, the same picture as H.264.)
        try:
            masked = await privacy.masked_recording(path, zones)
        except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
            raise HTTPException(status_code=503, detail="The masked recording could not be prepared") from exc
        finally:
            release_playback(path)
        await asyncio.to_thread(viewing_audit.safely, viewing_audit.touch_playback, user.id, camera_id)
        return FileResponse(masked, media_type="video/mp4", filename=path.name, content_disposition_type="inline")
    await asyncio.to_thread(viewing_audit.safely, viewing_audit.touch_playback, user.id, camera_id)
    return FileResponse(path, media_type="video/mp4", filename=path.name, content_disposition_type="inline", background=BackgroundTask(release_playback,path))


@router.get("/api/cameras/{camera_id}/segments/{segment_id}/snapshot")
@requires_scope("media")
async def snapshot_segment(
    camera_id: str,
    segment_id: int,
    at: datetime,
    user: CurrentUser,
    unmasked: Annotated[bool, Query()] = False,
) -> Response:
    if at.tzinfo is None:
        raise HTTPException(status_code=422, detail="Snapshot time must include a timezone")
    row, zones = await asyncio.to_thread(_segment_and_zones, "relative_path,starts_at,ends_at", segment_id, camera_id, user, unmasked)
    start = datetime.fromisoformat(row["starts_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    end = datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    at = at.astimezone(timezone.utc)
    if at < start or at >= end:
        raise HTTPException(status_code=422, detail="Snapshot time is outside this recorded interval")
    if not shutil.which("ffmpeg"):
        raise HTTPException(status_code=503, detail="Snapshot extraction is unavailable")
    from app.storage import recording_file_path
    try:
        path = await run_in_threadpool(recording_file_path, row["relative_path"])
    except (OSError, ValueError):
        raise HTTPException(status_code=410, detail="Recorded interval is unavailable") from None
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Recorded interval is unavailable") from None
    acquire_playback(path)
    offset = max(0.0, (at - start).total_seconds())
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{offset:.3f}",
            "-i", str(path), "-frames:v", "1", "-vf", privacy.with_mask("scale=1280:-2:force_original_aspect_ratio=decrease", zones),
            "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        image, _ = await run_to_end(process, timeout=15)
        if process.returncode != 0 or not image.startswith(b"\xff\xd8"):
            raise HTTPException(status_code=422, detail="A snapshot could not be extracted from this interval")
        source_time = at.isoformat()
        def note():
            with connect() as db:
                cursor = db.execute(
                    "INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                    (int(user["id"]), "snapshot_downloaded", "recording_segment", str(segment_id),
                     json.dumps({"camera_id": camera_id, "source_time": source_time})),
                )
                audit_reference = f"audit-{cursor.lastrowid}"
                viewing_audit.record_events(db, int(user["id"]), [camera_id], "snapshot", audit_reference)
            return audit_reference

        audit_reference = await asyncio.to_thread(note)
        return Response(image, media_type="image/jpeg", headers={
            "Cache-Control": "no-store", "X-Source-Time": source_time, "X-Audit-Reference": audit_reference,
        })
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Snapshot extraction timed out") from exc
    finally:
        release_playback(path)


@router.get("/api/cameras/{camera_id}/segments/{segment_id}/frame-neighbor")
@requires_scope("media")
def playback_frame_neighbor(
    camera_id: str,
    segment_id: int,
    at: datetime,
    direction: Literal["previous", "next"],
    _: CurrentUser,
) -> dict[str, str]:
    """Return an adjacent decoded video frame timestamp from a protected recording."""
    if at.tzinfo is None:
        raise HTTPException(status_code=422, detail="Frame time must include a timezone")
    with connect() as db:
        row = db.execute("SELECT relative_path,starts_at,ends_at FROM recording_segments WHERE id=? AND camera_id=?", (segment_id, camera_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Recorded interval is unavailable")
    start = datetime.fromisoformat(row["starts_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    end = datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    at = at.astimezone(timezone.utc)
    if at < start or at >= end:
        raise HTTPException(status_code=422, detail="Frame time is outside this recorded interval")
    if not shutil.which("ffprobe"):
        raise HTTPException(status_code=503, detail="Frame stepping is unavailable")
    from app.storage import recording_file_path
    try:
        path = recording_file_path(row["relative_path"])
    except (OSError, ValueError):
        raise HTTPException(status_code=410, detail="Recorded interval is unavailable") from None
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Recorded interval is unavailable") from None
    acquire_playback(path)
    try:
        offset = max(0.0, (at - start).total_seconds())
        window_start = max(0.0, offset - 2.0) if direction == "previous" else offset
        read_duration = offset - window_start + 2.0 if direction == "previous" else 2.0
        try:
            result = subprocess.run(
                [shutil.which("ffprobe"), "-v", "error", "-read_intervals", f"{window_start:.3f}%+{read_duration:.3f}",
                 "-select_streams", "v:0", "-show_frames", "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path)],
                capture_output=True, text=True, timeout=12, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=503, detail="Frame timestamp extraction timed out") from exc
        if result.returncode:
            raise HTTPException(status_code=422, detail="Frame timestamps could not be read")
        try:
            frames = json.loads(result.stdout).get("frames", [])
            frame_times = sorted({start.timestamp() + float(frame["best_effort_timestamp_time"]) for frame in frames
                                  if frame.get("best_effort_timestamp_time") is not None})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="Frame timestamps could not be read") from exc
        frame_times = [value for value in frame_times if start.timestamp() <= value < end.timestamp()]
        target = at.timestamp()
        candidates = [value for value in frame_times if value < target] if direction == "previous" else [value for value in frame_times if value > target]
        if not candidates:
            raise HTTPException(status_code=404, detail="No adjacent frame exists in this interval")
        selected = max(candidates) if direction == "previous" else min(candidates)
        return {"at": datetime.fromtimestamp(selected, timezone.utc).isoformat()}
    finally:
        release_playback(path)
