"""Administrator system status and opt-in support diagnostics."""
from __future__ import annotations

import os
import json

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.version import APP_VERSION
from app.principal import Administrator
from app.store import connect, database_path
from app import diagnostics

router = APIRouter()


class DiagnosticCaptureRequest(BaseModel):
    enabled: bool
    hours: int = Field(default=4, ge=1, le=24)


@router.get("/api/system/status")
def system_status(user: Administrator) -> dict[str, object]:
    db_path = database_path()
    with connect() as db:
        admin_count = db.execute("SELECT COUNT(*) FROM users WHERE role='administrator'").fetchone()[0]
        camera_count = db.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
    try:
        disk = os.statvfs(db_path.parent)
        storage = {"available_bytes": disk.f_bavail * disk.f_frsize, "total_bytes": disk.f_blocks * disk.f_frsize}
    except OSError:
        storage = {"available_bytes": None, "total_bytes": None}
    return {
        "application": "Workplace NVR",
        "version": APP_VERSION,
        "application_status": "healthy",
        "database": "connected",
        "configured_administrators": admin_count,
        "configured_cameras": camera_count,
        "storage": storage,
    }


@router.get("/api/diagnostics/status")
def diagnostics_status(_: Administrator) -> dict[str, object]:
    return diagnostics.capture_status()


@router.post("/api/diagnostics/capture")
def diagnostics_capture(
    payload: DiagnosticCaptureRequest,
    _: Administrator,
) -> dict[str, object]:
    return diagnostics.set_capture(payload.enabled, payload.hours)


@router.get("/api/diagnostics/bundle")
def diagnostics_bundle(_: Administrator) -> Response:
    return Response(
        content=json.dumps(diagnostics.bundle(), indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="nvr-diagnostics.json"', "Cache-Control": "no-store"},
    )
