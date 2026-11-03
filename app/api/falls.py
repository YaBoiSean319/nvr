"""Fall detection: which cameras look for falls, how sensitively, and the falls found."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import detection, falls, pose_models, rules as rules_engine
from app.audit import record
from app.principal import Administrator
from app.store import connect

router = APIRouter()


class CameraFalls(BaseModel):
    enabled: bool
    sensitivity: Literal["low", "normal", "high"] = falls.DEFAULT_SENSITIVITY
    stay_down_seconds: float = Field(default=falls.DEFAULT_STAY_DOWN_SECONDS, ge=falls.MIN_STAY_DOWN_SECONDS, le=falls.MAX_STAY_DOWN_SECONDS)


@router.get("/api/admin/falls")
def get_falls(_: Administrator) -> dict[str, Any]:
    """The pose model's state, each camera's setting, and the latest falls found."""
    with connect() as db:
        cameras = db.execute(
            "SELECT c.id,c.name,coalesce(f.enabled,0) AS enabled,coalesce(f.sensitivity,?) AS sensitivity,coalesce(f.stay_down_seconds,?) AS stay_down_seconds "
            "FROM cameras c LEFT JOIN fall_settings f ON f.camera_id=c.id WHERE c.archived_at IS NULL ORDER BY c.name COLLATE NOCASE,c.id",
            (falls.DEFAULT_SENSITIVITY, falls.DEFAULT_STAY_DOWN_SECONDS)).fetchall()
        recent = db.execute(
            "SELECT e.id,e.camera_id,c.name AS camera_name,e.occurred_at,e.down_seconds FROM fall_events e JOIN cameras c ON c.id=e.camera_id ORDER BY e.occurred_at DESC,e.id LIMIT 20").fetchall()
    cameras_out = []
    for row in cameras:
        config = detection.effective_camera_config(row["id"]) if row["enabled"] else None
        cameras_out.append({"id": row["id"], "name": row["name"], "enabled": bool(row["enabled"]), "sensitivity": row["sensitivity"], "stay_down_seconds": row["stay_down_seconds"],
                            # The rate the AI looks at this camera, and whether it is looking at all: a fall is a matter of seconds
                            "analysis_fps": float(config["analysis_fps"]) if config and config["enabled"] else None, "analysis_on": bool(config and config["enabled"])})
    with connect() as db:
        fall_rules = [rule for rule in rules_engine.load_rules(db) if rule.condition_type == "fall" and rule.enabled]
        unalerted = [camera["id"] for camera in cameras_out if camera["enabled"]
                     and not any(rule.watches_camera(camera["id"], rules_engine._camera_groups(db, camera["id"])) for rule in fall_rules)]
    return {"model": pose_models.status(),
            "defaults": {"sensitivity": falls.DEFAULT_SENSITIVITY, "stay_down_seconds": falls.DEFAULT_STAY_DOWN_SECONDS, "least_analysis_fps": falls.LEAST_ANALYSIS_FPS},
            "cameras": cameras_out,
            "unalerted_camera_ids": unalerted,  # Cameras looking for falls that no enabled rule alerts anyone about
            "recent": [{"id": row["id"], "camera_id": row["camera_id"], "camera_name": row["camera_name"], "occurred_at": row["occurred_at"], "down_seconds": row["down_seconds"]} for row in recent]}


@router.put("/api/admin/falls/cameras/{camera_id}")
def set_camera(camera_id: str, body: CameraFalls, user: Administrator) -> dict[str, Any]:
    """Turn fall detection on or off for a camera and set its sensitivity and stay-down time; the AI worker follows within about half a minute (no restart)."""
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if body.enabled and not pose_models.installed():
            raise HTTPException(status_code=409, detail="The pose model is not installed, so falls cannot be looked for; see docs/fall-detection.md")
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO fall_settings(camera_id,enabled,sensitivity,stay_down_seconds,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET "
                       "enabled=excluded.enabled,sensitivity=excluded.sensitivity,stay_down_seconds=excluded.stay_down_seconds,updated_at=excluded.updated_at",
                       (camera_id, int(body.enabled), body.sensitivity, body.stay_down_seconds, now))
            record(db, int(user["id"]), "falls_camera_changed", "camera", camera_id, enabled=body.enabled, sensitivity=body.sensitivity, stay_down_seconds=body.stay_down_seconds)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {"id": camera_id, "enabled": body.enabled, "sensitivity": body.sensitivity, "stay_down_seconds": body.stay_down_seconds}
