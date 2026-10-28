"""PTZ: moving a camera under the control lease, and administering its presets and tours.

Every command goes through the server; the browser never talks to the camera. Operators
may move cameras; presets and tours are for administrators.
"""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from app import ptz, ptz_tracking
from app.api.detections import recent_objects
from app.audit import record
from app.api.rules import RuleSchedule
from app.onvif_ptz import PtzUnsupported
from app.principal import Administrator, CurrentUser, Principal
from app.store import connect

router = APIRouter()

MAX_STEPS = 32


class MoveRequest(BaseModel):
    pan: float = Field(ge=-1, le=1)
    tilt: float = Field(ge=-1, le=1)
    zoom: float = Field(ge=-1, le=1)
    # The camera stops by itself after this long, even if the stop never arrives.
    seconds: float = Field(default=1.0, ge=0.1, le=10)


class CentreRequest(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class LeaseRequest(BaseModel):
    take_over: bool = False


class TrackRequest(BaseModel):
    track_id: str = Field(min_length=1, max_length=64)


class TrackingSettingsRequest(BaseModel):
    home_preset_id: str | None = Field(default=None, max_length=64)
    lost_seconds: float = Field(default=5, ge=1, le=60)
    home_timeout_seconds: int = Field(default=30, ge=5, le=3600)
    target_size: float = Field(default=0.4, ge=0.1, le=0.9)
    zoom_enabled: bool = True


class PresetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)

    @field_validator("name")
    @classmethod
    def trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a preset name")
        return value


class TourStep(BaseModel):
    preset_id: str = Field(min_length=1, max_length=64)
    dwell_seconds: int = Field(ge=3, le=3600)


class TourRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    enabled: bool = True
    steps: list[TourStep] = Field(min_length=1, max_length=MAX_STEPS)
    schedule: RuleSchedule | None = None

    @field_validator("name")
    @classmethod
    def trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a tour name")
        return value


def _client(camera_id: str):
    with connect() as db:
        try:
            return ptz.client_for(db, camera_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ptz.NotControllable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _command(camera_id: str, user: Principal, action) -> dict[str, object]:
    """Run a camera command for the lease holder, taking or renewing the lease first."""
    client = _client(camera_id)
    try:
        lease = ptz.acquire(camera_id, int(user["id"]))
    except ptz.LeaseHeld as exc:
        raise HTTPException(status_code=409, detail=f"{exc.holder} is controlling this camera") from exc
    try:
        await action(client)
    except PtzUnsupported as exc:
        raise HTTPException(status_code=409, detail="This camera has no PTZ service.") from exc
    except Exception as exc:  # noqa: BLE001 - never echo camera responses, which may carry details
        raise HTTPException(status_code=502, detail="The camera did not accept the command.") from exc
    return {"lease": lease}


@router.get("/api/cameras/{camera_id}/ptz")
async def get_ptz(camera_id: str, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=? AND archived_at IS NULL", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
    found, reason = await ptz.capabilities(camera_id)
    with connect() as db:
        return {"supported": found is not None, "reason": reason, "capabilities": ptz.capability_view(found),
                "lease": ptz.lease_view(db, camera_id, int(user["id"])), "lease_seconds": ptz.LEASE_SECONDS,
                "presets": ptz.presets(db, camera_id), "tours": ptz.tours(db, camera_id),
                "tracking": ptz_tracking.view(db.execute("SELECT * FROM ptz_tracking WHERE camera_id=?", (camera_id,)).fetchone()),
                "tracking_settings": ptz_tracking.settings_for(db, camera_id)}


@router.post("/api/cameras/{camera_id}/ptz/lease")
def take_lease(camera_id: str, payload: LeaseRequest, user: CurrentUser) -> dict[str, object]:
    if payload.take_over and not user.is_administrator:
        raise HTTPException(status_code=403, detail="Only administrators can take over control")
    _client(camera_id)
    try:
        return ptz.acquire(camera_id, int(user["id"]), take_over=payload.take_over)
    except ptz.LeaseHeld as exc:
        raise HTTPException(status_code=409, detail=f"{exc.holder} is controlling this camera") from exc


@router.delete("/api/cameras/{camera_id}/ptz/lease", status_code=status.HTTP_204_NO_CONTENT)
def release_lease(camera_id: str, user: CurrentUser) -> Response:
    ptz.release(camera_id, int(user["id"]))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/cameras/{camera_id}/ptz/move")
async def move(camera_id: str, payload: MoveRequest, user: CurrentUser) -> dict[str, object]:
    return await _command(camera_id, user, lambda client: client.continuous_move(payload.pan, payload.tilt, payload.zoom, payload.seconds))


@router.post("/api/cameras/{camera_id}/ptz/stop")
async def stop(camera_id: str, user: CurrentUser) -> dict[str, object]:
    return await _command(camera_id, user, lambda client: client.stop())


@router.post("/api/cameras/{camera_id}/ptz/centre")
async def centre(camera_id: str, payload: CentreRequest, user: CurrentUser) -> dict[str, object]:
    found, _ = await ptz.capabilities(camera_id)
    if found is None or not found.centre:
        raise HTTPException(status_code=409, detail="This camera cannot centre on a point.")
    return await _command(camera_id, user, lambda client: client.centre(payload.x, payload.y))


def _visible_camera(camera_id: str, user: Principal) -> None:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=? AND archived_at IS NULL", (camera_id,)).fetchone() is None or not user.access.can_see(camera_id):
            raise HTTPException(status_code=404, detail="Camera not found")


@router.get("/api/cameras/{camera_id}/ptz/targets")
def tracking_targets(camera_id: str, user: CurrentUser) -> dict[str, object]:
    """The objects in view now that the camera could follow, and the one it is following."""
    objects = recent_objects(user, camera_id)
    with connect() as db:
        tracking = ptz_tracking.view(db.execute("SELECT * FROM ptz_tracking WHERE camera_id=?", (camera_id,)).fetchone())
    return {"objects": objects, "tracking": tracking}


@router.post("/api/cameras/{camera_id}/ptz/track")
async def start_tracking(camera_id: str, payload: TrackRequest, user: CurrentUser) -> dict[str, object]:
    """Have the camera follow an object: it keeps it centred and sized, holding the control lease while it does."""
    _visible_camera(camera_id, user)
    try:
        tracking = await ptz_tracking.start(camera_id, payload.track_id, int(user["id"]), source="operator")
    except ptz_tracking.TrackingRefused as exc:
        raise HTTPException(status_code=409 if exc.code in {"lease_held", "already_tracking", "track_gone"} else 422, detail=str(exc)) from exc
    return {"tracking": tracking}


@router.delete("/api/cameras/{camera_id}/ptz/track", status_code=status.HTTP_204_NO_CONTENT)
async def stop_tracking(camera_id: str, user: CurrentUser) -> Response:
    """Stop following: the camera stops and the lease is let go. Anyone who may move the camera can."""
    _visible_camera(camera_id, user)
    with connect() as db:
        if db.execute("SELECT 1 FROM ptz_tracking WHERE camera_id=?", (camera_id,)).fetchone() is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
    await _command(camera_id, user, lambda client: client.stop())  # A manual command: it ends the tracking and stops the camera
    ptz.release(camera_id, int(user["id"]))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/api/cameras/{camera_id}/ptz/tracking")
def save_tracking_settings(camera_id: str, payload: TrackingSettingsRequest, user: Administrator) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT 1 FROM cameras WHERE id=? AND archived_at IS NULL", (camera_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="Camera not found")
            if payload.home_preset_id and db.execute("SELECT 1 FROM ptz_presets WHERE id=? AND camera_id=?", (payload.home_preset_id, camera_id)).fetchone() is None:
                raise HTTPException(status_code=422, detail="The home preset is not one of this camera's presets")
            db.execute("INSERT INTO ptz_tracking_settings(camera_id,home_preset_id,lost_seconds,home_timeout_seconds,target_size,zoom_enabled) VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(camera_id) DO UPDATE SET home_preset_id=excluded.home_preset_id,lost_seconds=excluded.lost_seconds,"
                       "home_timeout_seconds=excluded.home_timeout_seconds,target_size=excluded.target_size,zoom_enabled=excluded.zoom_enabled,"
                       "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                       (camera_id, payload.home_preset_id, payload.lost_seconds, payload.home_timeout_seconds, payload.target_size, int(payload.zoom_enabled)))
            record(db, int(user["id"]), "ptz_tracking_settings_changed", "camera", camera_id, **payload.model_dump())
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return ptz_tracking.settings_for(db, camera_id)


@router.post("/api/cameras/{camera_id}/ptz/presets/{preset_id}/go")
async def go_to_preset(camera_id: str, preset_id: str, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        preset = db.execute("SELECT token FROM ptz_presets WHERE id=? AND camera_id=?", (preset_id, camera_id)).fetchone()
    if preset is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    return await _command(camera_id, user, lambda client: client.goto_preset(preset["token"]))


@router.post("/api/cameras/{camera_id}/ptz/presets", status_code=status.HTTP_201_CREATED)
async def create_preset(camera_id: str, payload: PresetRequest, user: Administrator) -> dict[str, object]:
    """Save where the camera points now under a name."""
    with connect() as db:
        if db.execute("SELECT 1 FROM ptz_presets WHERE camera_id=? AND name=? COLLATE NOCASE", (camera_id, payload.name)).fetchone():
            raise HTTPException(status_code=409, detail="A preset with this name exists")
    client = _client(camera_id)
    try:
        token = await client.set_preset(payload.name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="The camera did not save the preset.") from exc
    preset_id = ptz.new_id()
    try:
        with connect() as db:
            db.execute("INSERT INTO ptz_presets(id,camera_id,name,token) VALUES(?,?,?,?)", (preset_id, camera_id, payload.name, token))
            return ptz.preset_view(db.execute("SELECT * FROM ptz_presets WHERE id=?", (preset_id,)).fetchone())
    except sqlite3.IntegrityError as exc:
        # Someone saved this name at the same moment: keep theirs and forget ours on the camera too.
        try:
            await client.remove_preset(token)
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(status_code=409, detail="A preset with this name exists") from exc


@router.patch("/api/cameras/{camera_id}/ptz/presets/{preset_id}")
def rename_preset(camera_id: str, preset_id: str, payload: PresetRequest, _: Administrator) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM ptz_presets WHERE camera_id=? AND name=? COLLATE NOCASE AND id<>?", (camera_id, payload.name, preset_id)).fetchone():
            raise HTTPException(status_code=409, detail="A preset with this name exists")
        if db.execute("UPDATE ptz_presets SET name=? WHERE id=? AND camera_id=?", (payload.name, preset_id, camera_id)).rowcount == 0:
            raise HTTPException(status_code=404, detail="Preset not found")
        return ptz.preset_view(db.execute("SELECT * FROM ptz_presets WHERE id=?", (preset_id,)).fetchone())


@router.delete("/api/cameras/{camera_id}/ptz/presets/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preset(camera_id: str, preset_id: str, _: Administrator) -> Response:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            preset = db.execute("SELECT token FROM ptz_presets WHERE id=? AND camera_id=?", (preset_id, camera_id)).fetchone()
            if preset is None:
                raise HTTPException(status_code=404, detail="Preset not found")
            for tour in db.execute("SELECT name,steps_json FROM ptz_tours WHERE camera_id=?", (camera_id,)):
                if any(step["preset_id"] == preset_id for step in json.loads(tour["steps_json"])):
                    raise HTTPException(status_code=409, detail=f"The tour {tour['name']} uses this preset")
            db.execute("DELETE FROM ptz_presets WHERE id=?", (preset_id,))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    # Best effort on the camera: a camera that is unreachable, or no longer ONVIF, keeps a stale preset nobody can pick.
    try:
        with connect() as db:
            client = ptz.client_for(db, camera_id)
        await client.remove_preset(preset["token"])
    except Exception:  # noqa: BLE001
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _save_tour(camera_id: str, tour_id: str, payload: TourRequest, *, create: bool) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT 1 FROM cameras WHERE id=? AND archived_at IS NULL", (camera_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="Camera not found")
            known = {row["id"] for row in db.execute("SELECT id FROM ptz_presets WHERE camera_id=?", (camera_id,))}
            if any(step.preset_id not in known for step in payload.steps):
                raise HTTPException(status_code=422, detail="A tour step names a preset this camera does not have")
            if payload.enabled and db.execute("SELECT 1 FROM ptz_tours WHERE camera_id=? AND enabled=1 AND id<>?", (camera_id, tour_id)).fetchone():
                raise HTTPException(status_code=409, detail="Another tour is enabled on this camera; disable it first")
            values = (payload.name, int(payload.enabled), json.dumps([step.model_dump() for step in payload.steps]),
                      json.dumps(payload.schedule.model_dump()) if payload.schedule else None)
            if create:
                db.execute("INSERT INTO ptz_tours(name,enabled,steps_json,schedule_json,id,camera_id) VALUES(?,?,?,?,?,?)", (*values, tour_id, camera_id))
            elif db.execute("UPDATE ptz_tours SET name=?,enabled=?,steps_json=?,schedule_json=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND camera_id=?",
                            (*values, tour_id, camera_id)).rowcount == 0:
                raise HTTPException(status_code=404, detail="Tour not found")
            # A changed tour starts again from its first step.
            db.execute("DELETE FROM ptz_tour_state WHERE tour_id=?", (tour_id,))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return ptz.tour_view(db, db.execute("SELECT * FROM ptz_tours WHERE id=?", (tour_id,)).fetchone())


@router.post("/api/cameras/{camera_id}/ptz/tours", status_code=status.HTTP_201_CREATED)
def create_tour(camera_id: str, payload: TourRequest, _: Administrator) -> dict[str, object]:
    return _save_tour(camera_id, ptz.new_id(), payload, create=True)


@router.put("/api/cameras/{camera_id}/ptz/tours/{tour_id}")
def update_tour(camera_id: str, tour_id: str, payload: TourRequest, _: Administrator) -> dict[str, object]:
    return _save_tour(camera_id, tour_id, payload, create=False)


@router.delete("/api/cameras/{camera_id}/ptz/tours/{tour_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tour(camera_id: str, tour_id: str, _: Administrator) -> Response:
    with connect() as db:
        db.execute("DELETE FROM ptz_tours WHERE id=? AND camera_id=?", (tour_id, camera_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
