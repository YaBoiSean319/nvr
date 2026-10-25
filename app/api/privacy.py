"""Privacy masks: each camera's masked areas, and administrators' audited unmasked viewing."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app import privacy
from app.principal import Administrator, CurrentUser
from app.store import connect

router = APIRouter()


class MaskArea(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)


class MasksRequest(BaseModel):
    zones: list[MaskArea] = Field(max_length=privacy.MAX_ZONES)


class UnmaskedRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=500)

    @field_validator("reason")
    @classmethod
    def meaningful(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 10:
            raise ValueError("Say why unmasked footage is needed, in at least 10 characters")
        return value


def _camera(camera_id: str) -> None:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")


@router.get("/api/cameras/{camera_id}/privacy")
def get_masks(camera_id: str, user: CurrentUser) -> dict[str, object]:
    _camera(camera_id)
    with connect() as db:
        view = privacy.status(db, camera_id)
        grant = privacy.active_grant(db, int(user["id"]), camera_id) if user.is_administrator and user.token_id is None else None
    return {**view, "unmasked_until": grant["expires_at"] if grant else None}


@router.put("/api/cameras/{camera_id}/privacy")
async def put_masks(camera_id: str, payload: MasksRequest, user: Administrator) -> dict[str, object]:
    try:
        zones = privacy.validate_zones([zone.model_dump() for zone in payload.zones])
        return await privacy.save(camera_id, zones, int(user["id"]))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/cameras/{camera_id}/privacy/unmasked")
def grant_unmasked(camera_id: str, payload: UnmaskedRequest, user: Administrator) -> dict[str, object]:
    """Let this administrator see the camera unmasked for a short while, for the reason given (audited)."""
    if user.token_id is not None:
        raise HTTPException(status_code=403, detail="Unmasked footage is for a signed-in administrator, never an API token")
    _camera(camera_id)
    return privacy.grant_unmasked(int(user["id"]), camera_id, payload.reason)
