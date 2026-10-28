"""Fisheye lenses (#146): which cameras are 360-degree cameras and how their lens is set, and the state of dewarped views kept in saved Live views."""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from app import dewarp
from app.audit import record
from app.principal import Administrator, CurrentUser
from app.store import connect

router = APIRouter()

MAX_VIEWS = 4


class LensRequest(BaseModel):
    kind: Literal["normal", "fisheye"]
    mount: Literal["ceiling", "wall", "floor"] = "ceiling"
    center_x: float = Field(default=0.5, ge=0, le=1)
    center_y: float = Field(default=0.5, ge=0, le=1)
    radius: float = Field(default=0.5, ge=0.1, le=1)  # A fraction of the picture's height
    fov: float = Field(default=180, ge=120, le=220)


class DewarpView(BaseModel):
    pan: float = Field(default=0, ge=-180, le=180)
    tilt: float = Field(default=-90, ge=-90, le=90)
    fov: float = Field(default=90, ge=dewarp.MIN_VIEW_FOV, le=dewarp.MAX_VIEW_FOV)


class DewarpState(BaseModel):
    """How one fisheye camera is shown in a Live view: raw, a panorama, a double panorama, or one to four virtual views."""
    mode: Literal["raw", "panorama", "double_panorama", "views"] = "raw"
    pan: float = Field(default=0, ge=-180, le=180)  # Where a panorama starts
    views: list[DewarpView] = Field(default_factory=list, max_length=MAX_VIEWS)

    @field_validator("views")
    @classmethod
    def views_for_mode(cls, value: list[DewarpView]) -> list[DewarpView]:
        return value


def clean_dewarp(states: dict[str, DewarpState] | None, camera_ids: list[str]) -> dict[str, dict[str, Any]]:
    """The dewarp states of the cameras a view holds, as plain data for storage; a state for a camera not in the view, or a views mode with no views, is refused."""
    cleaned: dict[str, dict[str, Any]] = {}
    for camera_id, state in (states or {}).items():
        if camera_id not in camera_ids:
            raise HTTPException(status_code=422, detail="A dewarped view names a camera that is not in the layout")
        if state.mode == "views" and not state.views:
            raise HTTPException(status_code=422, detail="Choose at least one virtual view")
        if state.mode != "raw":
            cleaned[camera_id] = state.model_dump()
    return cleaned


def decode_dewarp(text: str | None, camera_ids: list[str] | None = None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {key: state for key, state in value.items() if camera_ids is None or key in camera_ids}


def _view(row: Any) -> dict[str, Any]:
    return {"mount": row["mount"], "center_x": row["center_x"], "center_y": row["center_y"], "radius": row["radius"], "fov": row["fov"]}


@router.get("/api/cameras/lenses")
def list_lenses(user: CurrentUser) -> dict[str, object]:
    """The lens of every fisheye camera this person may see, keyed by camera id."""
    with connect() as db:
        rows = db.execute("SELECT l.* FROM camera_lens l JOIN cameras c ON c.id=l.camera_id WHERE c.archived_at IS NULL").fetchall()
    return {"lenses": {row["camera_id"]: _view(row) for row in rows if user.access.can_see(row["camera_id"])}}


@router.put("/api/cameras/{camera_id}/lens")
def set_lens(camera_id: str, payload: LensRequest, user: Administrator) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT 1 FROM cameras WHERE id=? AND archived_at IS NULL", (camera_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="Camera not found")
            if payload.kind == "normal":
                db.execute("DELETE FROM camera_lens WHERE camera_id=?", (camera_id,))
            else:
                db.execute("INSERT INTO camera_lens(camera_id,mount,center_x,center_y,radius,fov) VALUES(?,?,?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET "
                           "mount=excluded.mount,center_x=excluded.center_x,center_y=excluded.center_y,radius=excluded.radius,fov=excluded.fov,"
                           "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                           (camera_id, payload.mount, payload.center_x, payload.center_y, payload.radius, payload.fov))
            record(db, int(user["id"]), "camera_lens_changed", "camera", camera_id, **payload.model_dump())
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {"kind": payload.kind, **({k: v for k, v in payload.model_dump().items() if k != "kind"} if payload.kind == "fisheye" else {})}


@router.get("/api/cameras/{camera_id}/lens")
def get_lens(camera_id: str, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=? AND archived_at IS NULL", (camera_id,)).fetchone() is None or not user.access.can_see(camera_id):
            raise HTTPException(status_code=404, detail="Camera not found")
        row = db.execute("SELECT * FROM camera_lens WHERE camera_id=?", (camera_id,)).fetchone()
    return {"kind": "normal"} if row is None else {"kind": "fisheye", **_view(row)}
