"""Personal and shared live views and per-user live workspace state."""
from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field, model_validator

from app.viewing import MAX_LIVE_VIEWERS
from app.principal import Administrator, CurrentUser
from app.store import connect
from app.request_validation import normalize_live_grid_selection

router = APIRouter()


class ViewerLayoutRequest(BaseModel):
    camera_ids: list[str] = Field(default_factory=list, max_length=MAX_LIVE_VIEWERS)
    grid_size: int = Field(default=4)
    tile_camera_ids: list[str | None] | None = Field(default=None, max_length=MAX_LIVE_VIEWERS)
    layout_mode: Literal["auto", "manual"] = "auto"

    @model_validator(mode="after")
    def validate_layout(self) -> "ViewerLayoutRequest":
        self.camera_ids, self.grid_size, self.tile_camera_ids = normalize_live_grid_selection(
            self.camera_ids, self.grid_size, self.tile_camera_ids,
            allow_empty=False, empty_tiles_are_unset=False, duplicate_context="layout",
            overflow_error="Tile positions must match the selected grid size",
        )
        return self


class LiveWorkspaceRequest(BaseModel):
    camera_ids: list[str] = Field(default_factory=list, max_length=MAX_LIVE_VIEWERS)
    grid_size: int = Field(default=4)
    tile_camera_ids: list[str | None] = Field(default_factory=list, max_length=MAX_LIVE_VIEWERS)
    sidebar_collapsed: bool = False
    layout_mode: Literal["auto", "manual"] = "auto"

    @model_validator(mode="after")
    def validate_workspace(self) -> "LiveWorkspaceRequest":
        self.camera_ids, self.grid_size, self.tile_camera_ids = normalize_live_grid_selection(
            self.camera_ids, self.grid_size, self.tile_camera_ids,
            allow_empty=True, empty_tiles_are_unset=True, duplicate_context="workspace",
            overflow_error="Workspace cameras must fit the selected grid size",
        )
        return self


def _load_live_views(user: sqlite3.Row) -> dict[str, object]:
    with connect() as db:
        rows = db.execute(
            "SELECT name,camera_ids_json,grid_size,tile_camera_ids_json,layout_mode FROM viewer_layouts WHERE user_id=? ORDER BY name COLLATE NOCASE",
            (int(user["id"]),),
        ).fetchall()
        shared_rows = db.execute(
            "SELECT name,camera_ids_json,grid_size,tile_camera_ids_json,layout_mode FROM shared_viewer_layouts ORDER BY name COLLATE NOCASE"
        ).fetchall()
    def decode_layout(row: sqlite3.Row, scope: str) -> dict[str, object] | None:
        try:
            camera_ids = json.loads(row["camera_ids_json"])
            tile_camera_ids = json.loads(row["tile_camera_ids_json"] or "null")
            if not isinstance(camera_ids, list):
                return None
            if not isinstance(tile_camera_ids, list):
                tile_camera_ids = [*camera_ids, *([None] * max(0, int(row["grid_size"]) - len(camera_ids)))]
            return {"name": row["name"], "camera_ids": camera_ids, "grid_size": int(row["grid_size"]),
                    "tile_camera_ids": tile_camera_ids, "layout_mode": row["layout_mode"], "scope": scope}
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    layouts = []
    for row in rows:
        layout = decode_layout(row, "personal")
        if layout is not None:
            layouts.append(layout)
    shared_views = [layout for row in shared_rows if (layout := decode_layout(row, "shared")) is not None]
    return {"layouts": layouts, "shared_views": shared_views}


@router.get("/api/live/layouts")
def get_viewer_layouts(user: CurrentUser) -> dict[str, object]:
    views = _load_live_views(user)
    with connect() as db:
        available = {row["id"] for row in db.execute("SELECT id FROM cameras")}
    layouts = [{"name": layout["name"], "camera_ids": [camera_id for camera_id in layout["camera_ids"] if camera_id in available]}
               for layout in views["layouts"] if any(camera_id in available for camera_id in layout["camera_ids"])]
    return {"layouts": layouts}


@router.get("/api/live/views")
def get_live_views(user: CurrentUser) -> dict[str, object]:
    return _load_live_views(user)


@router.put("/api/live/layouts/{name}")
def save_viewer_layout(
    name: str,
    payload: ViewerLayoutRequest,
    user: CurrentUser,
) -> dict[str, object]:
    name = name.strip()
    if not name or len(name) > 60:
        raise HTTPException(status_code=422, detail="Layout name must be between 1 and 60 characters")
    with connect() as db:
        present = {row["id"] for row in db.execute("SELECT id FROM cameras").fetchall()}
        if not set(payload.camera_ids) <= present:
            raise HTTPException(status_code=422, detail="Layout contains an unknown camera")
        db.execute(
            "INSERT INTO viewer_layouts(user_id,name,camera_ids_json,updated_at,grid_size,tile_camera_ids_json,layout_mode) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id,name) DO UPDATE SET camera_ids_json=excluded.camera_ids_json,updated_at=excluded.updated_at,"
            "grid_size=excluded.grid_size,tile_camera_ids_json=excluded.tile_camera_ids_json,layout_mode=excluded.layout_mode",
            (int(user["id"]), name, json.dumps(payload.camera_ids), datetime.now(timezone.utc).isoformat(),
             payload.grid_size, json.dumps(payload.tile_camera_ids), payload.layout_mode),
        )
    return {"name": name, "camera_ids": payload.camera_ids}


@router.put("/api/live/shared-layouts/{name}")
def save_shared_viewer_layout(
    name: str, payload: ViewerLayoutRequest, user: Administrator,
) -> dict[str, object]:
    name = name.strip()
    if not name or len(name) > 60:
        raise HTTPException(status_code=422, detail="Layout name must be between 1 and 60 characters")
    with connect() as db:
        present = {row["id"] for row in db.execute("SELECT id FROM cameras").fetchall()}
        if not set(payload.camera_ids) <= present:
            raise HTTPException(status_code=422, detail="Layout contains an unknown camera")
        db.execute(
            "INSERT INTO shared_viewer_layouts(name,camera_ids_json,grid_size,tile_camera_ids_json,layout_mode,created_by,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET camera_ids_json=excluded.camera_ids_json,"
            "grid_size=excluded.grid_size,tile_camera_ids_json=excluded.tile_camera_ids_json,created_by=excluded.created_by,"
            "layout_mode=excluded.layout_mode,"
            "updated_at=excluded.updated_at",
            (name, json.dumps(payload.camera_ids), payload.grid_size, json.dumps(payload.tile_camera_ids), payload.layout_mode,
             int(user["id"]), datetime.now(timezone.utc).isoformat()),
        )
    return {"name": name, "camera_ids": payload.camera_ids, "grid_size": payload.grid_size,
            "tile_camera_ids": payload.tile_camera_ids, "layout_mode": payload.layout_mode, "scope": "shared"}


@router.delete("/api/live/shared-layouts/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shared_viewer_layout(name: str, user: Administrator) -> Response:
    with connect() as db:
        db.execute("DELETE FROM shared_viewer_layouts WHERE name=? COLLATE NOCASE", (name.strip(),))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/live/workspace")
def get_live_workspace(user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        row = db.execute("SELECT * FROM live_workspace_states WHERE user_id=?", (int(user["id"]),)).fetchone()
    if row is None:
        return {"camera_ids": None, "tile_camera_ids": [], "grid_size": 4, "layout_mode": "auto", "sidebar_collapsed": False}
    try:
        camera_ids = json.loads(row["camera_ids_json"])
        tile_camera_ids = json.loads(row["tile_camera_ids_json"])
    except (TypeError, json.JSONDecodeError):
        camera_ids, tile_camera_ids = [], []
    return {"camera_ids": camera_ids, "tile_camera_ids": tile_camera_ids, "grid_size": row["grid_size"],
            "layout_mode": row["layout_mode"], "sidebar_collapsed": bool(row["sidebar_collapsed"])}


@router.put("/api/live/workspace")
def save_live_workspace(payload: LiveWorkspaceRequest, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        present = {row["id"] for row in db.execute("SELECT id FROM cameras").fetchall()}
        if not set(payload.camera_ids) <= present:
            raise HTTPException(status_code=422, detail="Workspace contains an unknown camera")
        db.execute(
            "INSERT INTO live_workspace_states(user_id,camera_ids_json,tile_camera_ids_json,grid_size,sidebar_collapsed,updated_at,layout_mode) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET camera_ids_json=excluded.camera_ids_json,"
            "tile_camera_ids_json=excluded.tile_camera_ids_json,grid_size=excluded.grid_size,"
            "sidebar_collapsed=excluded.sidebar_collapsed,updated_at=excluded.updated_at,layout_mode=excluded.layout_mode",
            (int(user["id"]), json.dumps(payload.camera_ids), json.dumps(payload.tile_camera_ids), payload.grid_size,
             int(payload.sidebar_collapsed), datetime.now(timezone.utc).isoformat(), payload.layout_mode),
        )
    return {"camera_ids": payload.camera_ids, "tile_camera_ids": payload.tile_camera_ids,
            "grid_size": payload.grid_size, "layout_mode": payload.layout_mode, "sidebar_collapsed": payload.sidebar_collapsed}


@router.delete("/api/live/layouts/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_viewer_layout(name: str, user: CurrentUser) -> Response:
    with connect() as db:
        db.execute("DELETE FROM viewer_layouts WHERE user_id=? AND name=?", (int(user["id"]), name))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
