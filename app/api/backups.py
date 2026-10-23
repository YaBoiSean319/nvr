"""Configuration backup status and on-demand backups."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from app.backup import BackupError, queue_backup, status as backup_status
from app.principal import Administrator

router = APIRouter()


@router.get("/api/backups")
def get_backups(_: Administrator) -> dict[str, object]:
    return backup_status()


@router.post("/api/backups", status_code=status.HTTP_202_ACCEPTED)
async def post_backup(request: Request, user: Administrator) -> dict[str, object]:
    # Async so the wake-up runs on the event loop that owns the backup manager.
    try:
        run = queue_backup(int(user["id"]))
    except BackupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    backups = getattr(request.app.state, "backups", None)
    if backups is not None:
        backups.nudge()
    return {"run": run}
