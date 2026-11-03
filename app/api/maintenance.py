"""Maintenance windows (#234): periods in which cameras being worked on raise no outward alerts. Anyone who can see cameras may open a short window on them; administrators may cover groups or the whole site."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app import maintenance
from app.audit import record
from app.principal import CurrentUser
from app.store import connect

router = APIRouter()

RECENT = timedelta(days=30)


class WindowInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=500)
    scope: str = Field(default="cameras", max_length=16)
    camera_ids: list[str] = Field(default_factory=list, max_length=maintenance.MAX_CAMERAS)
    group_ids: list[str] = Field(default_factory=list, max_length=200)
    starts_at: datetime | None = None  # Left out: now
    ends_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 7)

    @model_validator(mode="after")
    def one_end(self) -> "WindowInput":
        if (self.ends_at is None) == (self.duration_minutes is None):
            raise ValueError("Give either an end time or a number of minutes")
        if (self.starts_at and self.starts_at.tzinfo is None) or (self.ends_at and self.ends_at.tzinfo is None):
            raise ValueError("Times need a time zone")
        return self


def _visible(db, user: CurrentUser, row) -> dict[str, Any] | None:
    """The window as this person may see it, or None when it concerns nothing they can see (and they did not open it)."""
    view = maintenance.view(db, row)
    if user["role"] == "administrator":
        return view
    pairs = [(item, name) for item, name in zip(view["camera_ids"], view["camera_names"]) if user.access.can_see(item)]
    mine = row["created_by"] == int(user["id"])
    if row["scope"] == "cameras" and not pairs and not mine:
        return None
    group_pairs = [(item, name) for item, name in zip(view["group_ids"], view["group_names"]) if user.access.can_see_group(item)]
    if row["scope"] == "groups" and not group_pairs and not mine:
        return None
    if row["scope"] == "groups":
        view["group_ids"], view["group_names"] = [item for item, _ in group_pairs], [name for _, name in group_pairs]
    if row["scope"] == "cameras":
        view["camera_ids"], view["camera_names"] = [item for item, _ in pairs], [name for _, name in pairs]  # Only the cameras this person may see are named
    return view


@router.get("/api/maintenance")
def list_windows(user: CurrentUser) -> dict[str, Any]:
    now = maintenance.iso(datetime.now(timezone.utc))
    since = maintenance.iso(datetime.now(timezone.utc) - RECENT)
    with connect() as db:
        rows = db.execute("SELECT * FROM maintenance_windows WHERE COALESCE(ended_at,ends_at)>? ORDER BY starts_at DESC LIMIT 300", (since,)).fetchall()
        views = [item for item in (_visible(db, user, row) for row in rows) if item is not None]
        site = any(item["state"] == "active" and item["scope"] == "site" for item in views)
        covered = [camera["id"] for camera in db.execute("SELECT id FROM cameras") if user.access.can_see(camera["id"]) and maintenance.covering(db, camera["id"]) is not None]
    order = {"active": 0, "upcoming": 1, "ended": 2}
    views.sort(key=lambda item: (order[item["state"]], item["starts_at"] if item["state"] != "ended" else ""), reverse=False)
    return {"now": now, "windows": views, "site_active": site, "covered_camera_ids": covered,
            "limits": {"operator_hours": int(maintenance.MAX_OPERATOR_LENGTH.total_seconds() // 3600), "administrator_days": maintenance.MAX_ADMIN_LENGTH.days}}


@router.post("/api/maintenance", status_code=201)
def open_window(payload: WindowInput, user: CurrentUser) -> dict[str, Any]:
    start = payload.starts_at or datetime.now(timezone.utc)
    end = payload.ends_at or start + timedelta(minutes=payload.duration_minutes or 0)
    administrator = user["role"] == "administrator"
    try:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                window_id = maintenance.create(
                    db, name=payload.name, reason=payload.reason, scope=payload.scope, camera_ids=payload.camera_ids, group_ids=payload.group_ids, starts_at=start, ends_at=end,
                    actor_id=int(user["id"]), actor_name=str(user["username"]), administrator=administrator, can_see=user.access.can_see)
                record(db, int(user["id"]), "maintenance_opened", "maintenance_window", window_id, name=payload.name.strip(), scope=payload.scope,
                       cameras=len(payload.camera_ids), groups=len(payload.group_ids), starts_at=maintenance.iso(start), ends_at=maintenance.iso(end))
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
            return maintenance.view(db, db.execute("SELECT * FROM maintenance_windows WHERE id=?", (window_id,)).fetchone())
    except maintenance.WindowError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/api/maintenance/{window_id}/end")
def end_window(window_id: str, user: CurrentUser) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM maintenance_windows WHERE id=?", (window_id,)).fetchone()
        if row is None or (user["role"] != "administrator" and row["created_by"] != int(user["id"])):
            raise HTTPException(status_code=404, detail="Maintenance window not found")  # Someone else's window is not found, like a hidden camera
        db.execute("BEGIN IMMEDIATE")
        try:
            was = maintenance.end_now(db, window_id)
            record(db, int(user["id"]), "maintenance_ended", "maintenance_window", window_id, name=was["name"], early=True,
                   held_alarms=was["suppressed_alarms"], held_incidents=was["suppressed_incidents"])
        except maintenance.WindowError as exc:
            db.execute("ROLLBACK")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        after = db.execute("SELECT * FROM maintenance_windows WHERE id=?", (window_id,)).fetchone()
        return maintenance.view(db, after) if after is not None else {"id": window_id, "state": "removed"}
