"""Camera capability health: what works on a camera and what does not, and a recheck on demand."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from app import capabilities
from app.audit import record
from app.principal import Administrator, CurrentUser
from app.store import connect

router = APIRouter()


def _camera_exists(camera_id: str) -> None:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")


@router.get("/api/cameras/{camera_id}/capabilities")
def get_capabilities(camera_id: str, _: CurrentUser) -> dict[str, object]:
    _camera_exists(camera_id)
    with connect() as db:
        return {"capabilities": capabilities.view(db, camera_id)}


@router.post("/api/cameras/{camera_id}/capabilities/recheck")
async def recheck_capabilities(camera_id: str, admin: Administrator) -> dict[str, object]:
    """Test video and motion events again now, instead of waiting for the scheduled recheck."""
    _camera_exists(camera_id)
    await asyncio.gather(*(capabilities.recheck(camera_id, name) for name in capabilities.RECHECKED))
    with connect() as db:
        record(db, admin.id, "camera_capabilities_rechecked", "camera", camera_id)
        return {"capabilities": capabilities.view(db, camera_id)}
