"""Inspect a camera from its address and login: who it is, how it signs in, its streams, and whether its video decodes."""
from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
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
    await asyncio.to_thread(_note, admin.id, payload.host, result)
    return result


def _note(actor_id: int, host: str, result: dict[str, object]) -> None:
    with connect() as db:
        record(db, actor_id, "camera_inspected", "camera", host, ok=bool(result["ok"]),
               failed=[step["id"] for step in result["steps"] if step["state"] == "failed"])


@router.post("/api/cameras/inspect/stream")
async def inspect_camera_live(payload: InspectRequest, admin: Administrator) -> StreamingResponse:
    """The same check, told as it happens (#173): one JSON object a line. First ``{"type": "steps"}`` with every step waiting, then
    ``{"type": "step"}`` as each step's outcome is known, and last ``{"type": "report"}`` with the whole report (as ``POST /api/cameras/inspect``
    answers) or ``{"type": "error"}``. A check takes up to a minute; this way the page shows each step when it is done. Closing the page ends
    the check."""
    updates: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    async def check() -> None:
        try:
            result = await camera_inspect.inspect_camera(
                payload.host, payload.username or "", payload.password or "", device_service_url=payload.device_service_url,
                brand=payload.camera_brand, verify=payload.verify_video, on_step=lambda step: updates.put_nowait({"type": "step", "step": step}))
        except ValueError as exc:
            updates.put_nowait({"type": "error", "detail": str(exc)})
            return
        await asyncio.to_thread(_note, admin.id, payload.host, result)
        updates.put_nowait({"type": "report", "report": result})

    async def lines():
        steps = [{"id": key, "label": label, "state": "pending", "detail": ""} for key, label in camera_inspect.STEPS]
        yield json.dumps({"type": "steps", "steps": steps}) + "\n"
        task = asyncio.create_task(check(), name="camera-inspect-live")
        try:
            while True:
                update = await updates.get()
                yield json.dumps(update) + "\n"
                if update["type"] in ("report", "error"):
                    return
        finally:
            if not task.done():  # The page went away: the check is not finished for nobody
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # Not buffered on the way: a proxy that holds the answer until it ends would show every step at once again.
    return StreamingResponse(lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
