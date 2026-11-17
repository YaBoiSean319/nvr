"""Bulk camera adding as a job the server runs (#173): start one, watch it, stop it. Administrators only."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from app import bulk_add
from app.principal import Administrator
from app.store import connect

router = APIRouter()


class BulkCamera(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=1024)
    name: str | None = Field(default=None, max_length=120)


class BulkAddRequest(BaseModel):
    cameras: list[BulkCamera] = Field(min_length=1, max_length=bulk_add.MAX_CAMERAS)
    add_anyway: bool = True  # Also add cameras that need attention (recording stays off)
    recording: bool = False  # Turn on motion recording for those that check out completely

    @model_validator(mode="after")
    def both_or_neither(self) -> "BulkAddRequest":
        for number, camera in enumerate(self.cameras, start=1):
            if bool(camera.username) != bool(camera.password):
                raise ValueError(f"Camera {number}: provide both username and password, or leave both blank")
        return self


def _view(job_id: str) -> dict[str, Any]:
    with connect() as db:
        view = bulk_add.job_view(db, job_id)
    if view is None:
        raise HTTPException(status_code=404, detail="No such bulk add")
    return view


@router.post("/api/cameras/bulk-jobs", status_code=status.HTTP_201_CREATED)
async def start_bulk_add(payload: BulkAddRequest, request: Request, admin: Administrator) -> dict[str, Any]:
    """Store the list and start working through it. The answer is the job as it stands; ``GET`` it to follow the progress. Passwords are kept encrypted until each camera is done."""
    job_id = bulk_add.create_job([camera.model_dump() for camera in payload.cameras], created_by=admin.id, add_anyway=payload.add_anyway, recording=payload.recording)
    request.app.state.bulk_add.wake()
    return _view(job_id)


@router.get("/api/cameras/bulk-jobs/latest")
async def latest_bulk_add(_: Administrator) -> dict[str, Any]:
    """The job still running, or the newest one finished within the last hour, so a page opened again can pick it up."""
    with connect() as db:
        job_id = bulk_add.latest_job_id(db)
        return {"job": bulk_add.job_view(db, job_id) if job_id else None}


@router.get("/api/cameras/bulk-jobs/{job_id}")
async def bulk_add_job(job_id: str, _: Administrator) -> dict[str, Any]:
    return _view(job_id)


@router.post("/api/cameras/bulk-jobs/{job_id}/cancel")
async def cancel_bulk_add(job_id: str, _: Administrator) -> dict[str, Any]:
    if not bulk_add.cancel_job(job_id):
        raise HTTPException(status_code=404, detail="No such bulk add")
    return _view(job_id)
