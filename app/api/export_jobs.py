"""Background export jobs, downloads and evidence verification."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, model_validator

from app.principal import CurrentUser, requires_scope
from app.store import connect
from app.retention import acquire_playback, release_playback
from app.exports import create_export_record, export_output_path, get_export, list_exports, request_cancel, verify_evidence_bytes, _job_view, MAX_EXPORT_CAMERAS, MAX_EXPORT_WINDOW, MAX_VERIFY_PACKAGE_BYTES

router = APIRouter()


class ExportRequest(BaseModel):
    camera_ids: list[str] = Field(min_length=1, max_length=MAX_EXPORT_CAMERAS)
    start: datetime
    end: datetime
    output_modes: list[Literal["individual", "grid"]] = Field(default_factory=lambda: ["individual", "grid"], min_length=1, max_length=2)
    tile_camera_ids: list[str | None] = Field(default_factory=list, max_length=16)
    grid_resolution: Literal["1080p", "4k"] = "1080p"
    include_audio: bool = False
    audio_camera_id: str | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "ExportRequest":
        if len(set(self.camera_ids)) != len(self.camera_ids):
            raise ValueError("Choose each camera only once")
        if len(set(self.output_modes)) != len(self.output_modes):
            raise ValueError("Choose each export output only once")
        if self.include_audio and self.audio_camera_id is not None and self.audio_camera_id not in self.camera_ids:
            raise ValueError("Audio source must be one of the selected cameras")
        if self.audio_camera_id is not None and (not self.include_audio or "grid" not in self.output_modes):
            raise ValueError("A grid audio source requires audio and a synchronized grid output")
        if self.include_audio and "grid" in self.output_modes and not self.audio_camera_id:
            raise ValueError("Choose one selected camera as the grid audio source")
        if self.tile_camera_ids and (len(self.tile_camera_ids) not in {1, 4, 9, 16} or
                len([item for item in self.tile_camera_ids if item is not None]) != len(set(item for item in self.tile_camera_ids if item is not None)) or
                set(item for item in self.tile_camera_ids if item is not None) != set(self.camera_ids)):
            raise ValueError("Grid tiles must include each selected camera exactly once")
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError("Export interval must include timezone-aware start and end times")
        if self.end - self.start > MAX_EXPORT_WINDOW:
            raise ValueError("Exports are limited to 24 hours per job")
        return self


@router.get("/api/exports")
@requires_scope("export")
def get_exports(user: CurrentUser) -> dict[str, object]:
    return {"exports": list_exports(int(user["id"]), user["role"] == "administrator")}


@router.post("/api/exports", status_code=status.HTTP_202_ACCEPTED)
@requires_scope("export")
async def create_export(
    payload: ExportRequest,
    request: Request,
    user: CurrentUser,
) -> dict[str, object]:
    start, end = payload.start.astimezone(timezone.utc), payload.end.astimezone(timezone.utc)
    return _submit(request, user, lambda: create_export_record(
        int(user["id"]), payload.camera_ids, start, end, payload.output_modes, payload.tile_camera_ids,
        payload.grid_resolution, payload.include_audio, payload.audio_camera_id))


def _submit(request: Request, user: dict, create: Callable[[], str]) -> dict[str, object]:
    """Validate and record a job, then hand it to the export worker."""
    try:
        job_id = create()
    except LookupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        request.app.state.exports.enqueue(job_id)
    except (AttributeError, RuntimeError) as exc:
        with connect() as db:
            db.execute("UPDATE exports SET status='failed',error=?,completed_at=?,updated_at=? WHERE id=?", ("Export worker is unavailable", datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), job_id))
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)", (int(user["id"]), "export_failed", "export", job_id, '{"reason":"worker_unavailable"}'))
        raise HTTPException(status_code=503, detail="Export worker is unavailable") from exc
    row = get_export(job_id, int(user["id"]))
    return _job_view(row)


@router.post("/api/exports/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
@requires_scope("export")
async def retry_export(job_id: str, request: Request, user: CurrentUser) -> dict[str, object]:
    """Repeat a finished-unsuccessfully job as a new attempt; the original stays as it was."""
    row = get_export(job_id, int(user["id"]), user["role"] == "administrator")
    if row is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    if row["status"] not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Only failed or cancelled exports can be retried")
    original = _job_view(row)
    return _submit(request, user, lambda: create_export_record(
        int(user["id"]), original["camera_ids"],
        datetime.fromisoformat(original["start"]).astimezone(timezone.utc), datetime.fromisoformat(original["end"]).astimezone(timezone.utc),
        original["output_modes"], original["tile_camera_ids"], original["grid_resolution"], original["include_audio"],
        original["audio_camera_id"], retry_of=job_id))


@router.get("/api/exports/{job_id}")
@requires_scope("export")
def get_export_status(job_id: str, user: CurrentUser) -> dict[str, object]:
    row = get_export(job_id, int(user["id"]), user["role"] == "administrator")
    if row is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    return _job_view(row)


@router.post("/api/exports/{job_id}/cancel")
@requires_scope("export")
async def cancel_export(job_id: str, request: Request, user: CurrentUser) -> dict[str, object]:
    try:
        state = request_cancel(job_id, int(user["id"]), user["role"] == "administrator")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if state == "cancel_requested":
        await request.app.state.exports.cancel(job_id)
    row = get_export(job_id, int(user["id"]), user["role"] == "administrator")
    return _job_view(row)


@router.get("/api/exports/{job_id}/download")
@requires_scope("export")
def download_export(job_id: str, user: CurrentUser) -> FileResponse:
    row = get_export(job_id, int(user["id"]), user["role"] == "administrator")
    if row is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    if row["status"] == "completed" and row["expires_at"] and datetime.now(timezone.utc) >= datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")):
        from app.exports import expire_exports
        expire_exports()
        raise HTTPException(status_code=410, detail="Evidence package expired after seven days")
    try:
        path = export_output_path(row)
    except FileNotFoundError as exc:
        status_code = 410 if row["status"] == "completed" else 409
        raise HTTPException(status_code=status_code, detail="Evidence package is unavailable" if status_code == 410 else str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=410, detail="Export file is unavailable") from exc
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Export file is unavailable")
    # A package made before a camera's privacy masks were set or changed carries footage they now hide.
    with connect() as db:
        for camera_id in json.loads(row["camera_ids_json"]):
            masks = db.execute("SELECT zones_json,updated_at FROM privacy_masks WHERE camera_id=?", (camera_id,)).fetchone()
            if masks and json.loads(masks["zones_json"]) and masks["updated_at"] > row["created_at"]:
                raise HTTPException(status_code=409, detail="This export was made before the camera's current privacy masks; export again to download it masked")
    acquire_playback(path)
    return FileResponse(path, media_type="application/zip", filename=f"nvr-export-{job_id}.zip", background=BackgroundTask(release_playback, path))


@router.post("/api/exports/{job_id}/verify")
@requires_scope("export")
async def verify_export_evidence(job_id: str, request: Request, user: CurrentUser) -> dict[str, object]:
    """Check manifest checksums against uploaded package bytes; media probing is separate."""
    row = get_export(job_id, int(user["id"]), user["role"] == "administrator")
    if row is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    if row["status"] != "completed":
        raise HTTPException(status_code=409, detail="Evidence package is not ready")
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_VERIFY_PACKAGE_BYTES:
        raise HTTPException(status_code=413, detail="Evidence package exceeds the verification size limit")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_VERIFY_PACKAGE_BYTES:
            raise HTTPException(status_code=413, detail="Evidence package exceeds the verification size limit")
    result = verify_evidence_bytes(bytes(body), job_id)
    action = "evidence_verified" if result["checksums_valid"] else "evidence_verification_failed"
    with connect() as db:
        db.execute(
            "INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
            (int(user["id"]), action, "export", job_id, json.dumps({"outcome": "valid" if result["checksums_valid"] else "invalid", "file_count": len(result["files"])})),
        )
    return {**result, "media_validation": "not_performed", "authenticity": "Checksums are unsigned. A matching manifest detects accidental or subsequent byte changes but does not prove package origin, creation time, or camera timestamp accuracy."}
