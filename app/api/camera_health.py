"""The camera health dashboard (#137): a fleet view of what cameras report and the NVR measures, and its CSV. Starts no media."""
from __future__ import annotations

from fastapi import APIRouter, Response

from app import camera_health
from app.principal import CurrentUser
from app.store import connect

router = APIRouter()


def _fleet(user) -> dict[str, object]:
    visible = None if user.access.unrestricted else set(user.access.cameras or ())
    with connect() as db:
        return camera_health.fleet(db, visible, show_errors=user.is_administrator)


@router.get("/api/camera-health")
def get_camera_health(user: CurrentUser) -> dict[str, object]:
    """Every camera this person may see with make, model, firmware, uptime, configured and measured bitrate and frame rate, dropped frames, reconnects and the last ONVIF contact."""
    return _fleet(user)


@router.get("/api/camera-health/export.csv")
def export_camera_health(user: CurrentUser) -> Response:
    return Response(camera_health.to_csv(_fleet(user)), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="camera-health.csv"', "Cache-Control": "no-store"})
