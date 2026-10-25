"""Camera tamper detection: its settings, state and the action to learn the view again."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import tamper
from app.audit import record
from app.principal import Administrator, CurrentUser
from app.store import connect

router = APIRouter()


class TamperSettings(BaseModel):
    enabled: bool
    sensitivity: int = Field(ge=1, le=100)
    sustain_seconds: int = Field(ge=10, le=3600)


def _camera(db, camera_id: str) -> None:
    row = db.execute("SELECT archived_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Camera not found")


@router.get("/api/cameras/{camera_id}/tamper")
def get_tamper(camera_id: str, _: CurrentUser) -> dict[str, object]:
    with connect() as db:
        _camera(db, camera_id)
        return tamper.view(db, camera_id)


@router.put("/api/cameras/{camera_id}/tamper")
def put_tamper(camera_id: str, payload: TamperSettings, admin: Administrator) -> dict[str, object]:
    with connect() as db:
        _camera(db, camera_id)
        before = tamper.view(db, camera_id)
        tamper.configure(db, camera_id, enabled=payload.enabled, sensitivity=payload.sensitivity, sustain_seconds=payload.sustain_seconds)
        record(db, admin.id, "tamper_configured", "camera", camera_id,
               before={key: before[key] for key in ("enabled", "sensitivity", "sustain_seconds")}, after=payload.model_dump())
        return tamper.view(db, camera_id)


@router.post("/api/cameras/{camera_id}/tamper/relearn")
def relearn_tamper(camera_id: str, admin: Administrator) -> dict[str, object]:
    """After an intentional change (the camera was moved, the scene rearranged): learn the view again and end any detection."""
    with connect() as db:
        _camera(db, camera_id)
        tamper.relearn(db, camera_id)
        record(db, admin.id, "tamper_relearned", "camera", camera_id)
        return tamper.view(db, camera_id)
