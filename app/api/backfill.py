"""Edge backfill settings and status per camera (#131)."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import backfill
from app.principal import Administrator, CurrentUser
from app.store import connect

router = APIRouter()


class BackfillSettings(BaseModel):
    enabled: bool
    limit_kbps: int | None = Field(default=None, ge=backfill.MIN_LIMIT_KBPS, le=backfill.MAX_LIMIT_KBPS)


def _camera(camera_id: str) -> None:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")


@router.get("/api/cameras/{camera_id}/backfill")
def get_backfill(camera_id: str, _: CurrentUser) -> dict[str, object]:
    """Whether the camera is backfilled, what is known about its Profile G support, and the recent jobs."""
    _camera(camera_id)
    return backfill.status(camera_id)


@router.put("/api/cameras/{camera_id}/backfill")
async def put_backfill(camera_id: str, payload: BackfillSettings, user: Administrator) -> dict[str, object]:
    """Turn backfill on or off and set its bandwidth limit. Turning it on has the camera checked afresh, so a camera without Profile G says so at once."""
    _camera(camera_id)
    with connect() as db:
        before = db.execute("SELECT backfill_enabled,backfill_limit_kbps FROM cameras WHERE id=?", (camera_id,)).fetchone()
        db.execute("UPDATE cameras SET backfill_enabled=?,backfill_limit_kbps=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (int(payload.enabled), payload.limit_kbps, camera_id))
        if payload.enabled and not before["backfill_enabled"]:
            # Switching it on again is how an administrator asks for a new look at a camera found unsupported; waiting jobs of that kind try again.
            db.execute("UPDATE cameras SET backfill_support=NULL,backfill_support_message=NULL,backfill_checked_at=NULL WHERE id=?", (camera_id,))
            db.execute("UPDATE backfill_jobs SET state='pending',message='',attempts=0,next_attempt_at=NULL WHERE camera_id=? AND state IN ('unsupported','failed')", (camera_id,))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "backfill_settings_changed", "camera", camera_id,
                    json.dumps({"enabled": payload.enabled, "limit_kbps": payload.limit_kbps, "was_enabled": bool(before["backfill_enabled"]), "was_limit_kbps": before["backfill_limit_kbps"]})))
    if payload.enabled and not before["backfill_enabled"]:
        try:
            await asyncio.wait_for(backfill.check_support(camera_id), 20)
        except Exception:  # noqa: BLE001 - a camera that cannot be asked now is checked when its first outage is filled
            pass
    return backfill.status(camera_id)


@router.post("/api/cameras/{camera_id}/backfill/check")
async def check_backfill(camera_id: str, _: Administrator) -> dict[str, object]:
    """Ask the camera now whether it offers ONVIF Profile G search and replay."""
    _camera(camera_id)
    await backfill.check_support(camera_id)
    return backfill.status(camera_id)
