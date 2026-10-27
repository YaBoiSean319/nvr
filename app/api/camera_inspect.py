"""Inspect a camera from its address and login: who it is, how it signs in, its streams, and whether its video decodes."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app import camera_inspect
from app.audit import record
from app.onvif_client import normalise_host
from app.principal import Administrator
from app.store import connect

router = APIRouter()

CameraBrand = Literal["auto", "acti", "axis", "bosch", "dahua", "hanwha", "hikvision", "reolink", "samsung", "uniview", "other"]


class InspectRequest(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=1024)
    device_service_url: str | None = Field(default=None, max_length=2048)
    camera_brand: CameraBrand = "auto"
    verify_video: bool = True

    @model_validator(mode="after")
    def validate_request(self) -> "InspectRequest":
        if bool(self.username) != bool(self.password):
            raise ValueError("Provide both camera username and password, or leave both blank")
        try:
            self.host = normalise_host(self.host)
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        return self


@router.post("/api/cameras/inspect")
async def inspect_camera(payload: InspectRequest, admin: Administrator) -> dict[str, object]:
    """Run every onboarding step against one camera and report each step's outcome. A camera that fails a step is still a 200 with the reason:
    nothing here saves anything, and the report never holds a credential. At most a few inspections run at once."""
    try:
        result = await camera_inspect.inspect_camera(
            payload.host, payload.username or "", payload.password or "", device_service_url=payload.device_service_url,
            brand=payload.camera_brand, verify=payload.verify_video)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    with connect() as db:
        record(db, admin.id, "camera_inspected", "camera", payload.host, ok=bool(result["ok"]),
               failed=[step["id"] for step in result["steps"] if step["state"] == "failed"])
    return result
