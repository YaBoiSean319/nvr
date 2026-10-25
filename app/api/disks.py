"""Disk health (SMART) and the retention forecast."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from app import forecast, smart
from app.audit import record
from app.principal import Administrator, CurrentUser
from app.storage import configured_volumes, volume_path
from app.store import connect

router = APIRouter()


@router.get("/api/storage/disk-health")
def get_disk_health(_: Administrator) -> dict[str, object]:
    with connect() as db:
        disks = smart.view(db)
    return {"disks": disks, "refresh_hours": round(smart.REFRESH_SECONDS / 3600, 2)}


@router.post("/api/storage/disk-health/refresh")
async def refresh_disk_health(admin: Administrator) -> dict[str, object]:
    """Read every volume's disk now, rather than at the next scheduled reading."""
    volumes = {}
    for volume_id in configured_volumes():
        try:
            volumes[volume_id] = volume_path(volume_id)
        except OSError:
            continue
    states = await asyncio.to_thread(smart.refresh, volumes)
    with connect() as db:
        record(db, admin.id, "disk_health_refreshed", "storage", "volumes", states=states)
        return {"disks": smart.view(db), "refresh_hours": round(smart.REFRESH_SECONDS / 3600, 2)}


@router.get("/api/storage/forecast")
def get_forecast(_: Administrator) -> dict[str, object]:
    with connect() as db:
        return forecast.forecast(db)


@router.get("/api/cameras/{camera_id}/retention-forecast")
def get_camera_forecast(camera_id: str, _: CurrentUser) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        result = forecast.forecast(db)
    camera = next(item for item in result["cameras"] if item["id"] == camera_id)
    return {"as_of": result["as_of"], "window_hours": result["window_hours"], "warn_days": result["warn_days"], "camera": camera,
            "disk": next((disk for disk in result["disks"] if camera["volume_id"] in disk["volume_ids"]), None)}
