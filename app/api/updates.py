"""Guided updates (#148): is a newer release out, and the host commands to upgrade or roll back.

Nothing here can pull an image or restart anything: the NVR has no Docker access, and these routes only read the release feed and the NVR's own state, start the
pre-update configuration backup, and return commands for an administrator to run on the host.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import updates
from app.backup import BackupError
from app.principal import Administrator
from app.store import connect

router = APIRouter()


class UpdateSettings(BaseModel):
    check_enabled: bool


class PrepareRequest(BaseModel):
    without_backup: bool = False


@router.get("/api/admin/updates")
def get_updates(_: Administrator) -> dict[str, object]:
    return updates.state()


@router.put("/api/admin/updates/settings")
def put_update_settings(payload: UpdateSettings, user: Administrator) -> dict[str, object]:
    with connect() as db:
        updates.set_check_enabled(db, user.id, payload.check_enabled)
    return updates.state()


@router.post("/api/admin/updates/check")
def post_check(user: Administrator) -> dict[str, object]:
    try:
        return updates.check(user.id)
    except updates.UpdatesDisabled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/admin/updates/prepare", status_code=202)
async def post_prepare(request: Request, payload: PrepareRequest, user: Administrator) -> dict[str, object]:
    # Async so the wake-up runs on the event loop that owns the backup manager.
    try:
        result = updates.prepare(user.id, without_backup=payload.without_backup)
    except (BackupError, updates.PlanNotReady) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    backups = getattr(request.app.state, "backups", None)
    if backups is not None and result["run"] is not None:
        backups.nudge()
    return result


@router.get("/api/admin/updates/plan")
def get_plan(_: Administrator) -> dict[str, object]:
    try:
        return updates.plan()
    except updates.PlanNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
