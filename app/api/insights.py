"""Activity insights: counts per camera, zone and line by hour or day, a heatmap of where objects go, and a CSV of the counts (#135)."""
from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool

from app import insights, privacy, search
from app.api.playback import _segment_zones
from app.principal import CurrentUser, requires_scope
from app.retention import acquire_playback, release_playback
from app.storage import recording_file_path
from app.store import connect

router = APIRouter()

Unit = Literal["hour", "day"]


def _range(start: str, end: str, unit: str | None = None) -> tuple[datetime, datetime]:
    try:
        start_time, end_time = datetime.fromisoformat(start.replace("Z", "+00:00")), datetime.fromisoformat(end.replace("Z", "+00:00"))
        if start_time.tzinfo is None or end_time.tzinfo is None:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="start and end must be timestamps with a time zone") from None
    limit = insights.MAX_RANGE[unit] if unit else search.MAX_RANGE
    if (end_time - start_time).total_seconds() <= 0 or (end_time - start_time) > limit:
        raise HTTPException(status_code=422, detail=f"The range must be positive and at most {limit.days} days" + (f" by the {unit}" if unit else ""))
    return start_time.astimezone(timezone.utc), end_time.astimezone(timezone.utc)


def _label(label: str | None) -> str | None:
    if label in (None, ""):
        return None
    if label not in insights.LABELS:
        raise HTTPException(status_code=422, detail=f"label must be one of {', '.join(insights.LABELS)}")
    return label


def _report(user, start: str, end: str, unit: str, camera_ids: list[str] | None, label: str | None) -> dict[str, object]:
    start_time, end_time = _range(start, end, unit)
    with connect() as db:
        names = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM cameras WHERE archived_at IS NULL ORDER BY name COLLATE NOCASE")}
        # Cameras this person may not see are not counted, and cannot be told apart from cameras that do not exist.
        wanted = [camera_id for camera_id in (camera_ids or names) if camera_id in names]
        visible = {camera_id: names[camera_id] for camera_id in user.access.only(wanted)}
        return {"start": start_time.isoformat(), "end": end_time.isoformat(),
                **insights.counts(db, visible, start_time, end_time, unit, label=_label(label))}


@router.get("/api/insights")
def get_insights(
    user: CurrentUser,
    start: str = Query(max_length=64),
    end: str = Query(max_length=64),
    unit: Unit = "hour",
    camera_ids: list[str] | None = Query(default=None, max_length=500),
    label: str | None = Query(default=None, max_length=32),
) -> dict[str, object]:
    """Counts of people and vehicles per hour or day for each camera, and for each zone and line drawn in a rule on it."""
    return _report(user, start, end, unit, camera_ids, label)


@router.get("/api/insights/export.csv")
def export_insights(
    user: CurrentUser,
    start: str = Query(max_length=64),
    end: str = Query(max_length=64),
    unit: Unit = "hour",
    camera_ids: list[str] | None = Query(default=None, max_length=500),
    label: str | None = Query(default=None, max_length=32),
) -> Response:
    report = _report(user, start, end, unit, camera_ids, label)
    return Response(insights.to_csv(report), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="insights.csv"', "Cache-Control": "no-store"})


@router.get("/api/insights/heatmap")
def get_heatmap(
    camera_id: str,
    user: CurrentUser,
    start: str = Query(max_length=64),
    end: str = Query(max_length=64),
    label: str | None = Query(default=None, max_length=32),
) -> dict[str, object]:
    start_time, end_time = _range(start, end)
    with connect() as db:
        row = db.execute("SELECT name FROM cameras WHERE id=? AND archived_at IS NULL", (camera_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        return insights.heatmap(db, camera_id, row["name"], start_time, end_time, label=_label(label))


@router.get("/api/insights/reference")
@requires_scope("media")
async def reference_frame(camera_id: str, user: CurrentUser, at: str | None = Query(default=None, max_length=64)) -> Response:
    """A still from a recording of the camera (the latest before ``at``, or the latest there is), with privacy masks applied, to draw the heatmap over."""
    try:
        moment = datetime.fromisoformat(at.replace("Z", "+00:00")) if at else datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="at must be a timestamp with a time zone") from None
    with connect() as db:
        row = db.execute("SELECT relative_path,starts_at,ends_at FROM recording_segments WHERE camera_id=? AND starts_at<=? ORDER BY starts_at DESC LIMIT 1",
                         (camera_id, moment.astimezone(timezone.utc).isoformat())).fetchone() or db.execute(
            "SELECT relative_path,starts_at,ends_at FROM recording_segments WHERE camera_id=? ORDER BY starts_at DESC LIMIT 1", (camera_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="There is no recording to draw over")
    zones = _segment_zones(user, camera_id, row["starts_at"], False)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(status_code=503, detail="A still cannot be extracted without FFmpeg")
    try:
        path = await run_in_threadpool(recording_file_path, row["relative_path"])
    except (OSError, ValueError):
        raise HTTPException(status_code=410, detail="The recording is unavailable") from None
    if not path.is_file():
        raise HTTPException(status_code=410, detail="The recording is unavailable")
    acquire_playback(path)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", "1", "-i", str(path), "-frames:v", "1",
            "-vf", privacy.with_mask("scale=960:-2:force_original_aspect_ratio=decrease", zones), "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        image, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        if process.returncode != 0 or not image.startswith(b"\xff\xd8"):
            raise HTTPException(status_code=422, detail="A still could not be extracted from the recording")
        return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Source-Time": row["starts_at"]})
    except asyncio.TimeoutError:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        raise HTTPException(status_code=503, detail="Extracting a still timed out") from None
    finally:
        release_playback(path)
