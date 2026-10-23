"""Per-camera, per-group and runtime person/vehicle detection settings."""
from __future__ import annotations

import asyncio
import sqlite3
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.principal import Administrator, CurrentUser
from app.store import connect
from app.detection import detector_availability, effective_camera_config, model_options, openvino_provider_options, validate_detector, MODEL_ID, MODEL_RUNTIME, MODEL_VERSION
from app.api.cameras import MotionZoneRequest

router = APIRouter()


class AIDetectionConfigRequest(BaseModel):
    enabled: bool = False
    analysis_fps: float = Field(default=0.5, ge=0.1, le=5)
    confidence_threshold: float = Field(default=0.55, ge=0.1, le=0.99)
    use_substream: bool = True
    zones: list[MotionZoneRequest] = Field(default_factory=list, max_length=16)
    inherit_group: bool = False


class AIRuntimeSettingsRequest(BaseModel):
    max_active_cameras: int = Field(ge=1, le=16)
    provider: Literal["CPU", "GPU"] | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=128)


class AIRuntimeValidationRequest(BaseModel):
    provider: Literal["CPU", "GPU"]
    model_id: str = Field(min_length=1, max_length=128)


def _ensure_ai_settings(camera_id: str) -> sqlite3.Row:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        db.execute("INSERT OR IGNORE INTO ai_camera_settings(camera_id) VALUES(?)", (camera_id,))
        row = db.execute("SELECT * FROM ai_camera_settings WHERE camera_id=?", (camera_id,)).fetchone()
    return row


def _ai_runtime_model_view() -> dict[str, object]:
    with connect() as db:
        runtime = db.execute("SELECT provider,model_id,model_version,validated_at FROM ai_runtime_settings WHERE singleton_id=1").fetchone()
    provider = str(runtime["provider"]) if runtime else "CPU"
    model_id = str(runtime["model_id"]) if runtime else MODEL_ID
    stored_version = str(runtime["model_version"]) if runtime else MODEL_VERSION
    options = model_options()
    selected = next((item for item in options if item["id"] == model_id), None)
    content_matches = bool(selected and selected["available"] and selected["version"] == stored_version)
    availability = detector_availability(provider, model_id)
    validated = bool(runtime and runtime["validated_at"] and content_matches)
    return {
        "provider": provider,
        "model_id": model_id,
        "model_version": stored_version,
        "runtime": f"{MODEL_RUNTIME} {provider}",
        "available": bool(availability["available"] and content_matches),
        "reason": availability["reason"] if content_matches else "Model files changed since validation; validate the selected model before activation",
        "validated": validated,
        "validated_at": runtime["validated_at"] if runtime else None,
        "provider_options": openvino_provider_options(),
        "model_options": options,
    }


@router.get("/api/cameras/{camera_id}/ai")
def get_ai_detection_config(camera_id: str, _: CurrentUser) -> dict[str, object]:
    row = _ensure_ai_settings(camera_id)
    effective = effective_camera_config(camera_id) or {}
    return {
        "camera_id": camera_id,
        "enabled": bool(effective.get("enabled", False)),
        "analysis_fps": effective.get("analysis_fps", 0.5),
        "confidence_threshold": effective.get("confidence_threshold", 0.55),
        "use_substream": bool(effective.get("use_substream", True)),
        "zones": json.loads(effective.get("zones_json", "[]")),
        "config_source": effective.get("config_source", "default"),
        "config_override": bool(row["config_override"]),
        "status": row["status"],
        "last_attempt_at": row["last_attempt_at"],
        "last_success_at": row["last_success_at"],
        "last_error": row["last_error"],
        "model": _ai_runtime_model_view(),
        "coverage_note": "No detections do not prove an object was absent; inspect AI coverage status and model results.",
    }


@router.put("/api/cameras/{camera_id}/ai")
async def update_ai_detection_config(
    camera_id: str,
    payload: AIDetectionConfigRequest,
    request: Request,
    user: Administrator,
) -> dict[str, object]:
    existing = _ensure_ai_settings(camera_id)
    zones = [zone.model_dump() for zone in payload.zones]
    active_model = _ai_runtime_model_view()
    with connect() as db:
        camera = db.execute("SELECT group_id,live_substream_url_ciphertext FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if payload.inherit_group and not camera["group_id"]:
            raise HTTPException(status_code=422, detail="Assign this camera to a group before inheriting its AI settings")
        if not payload.inherit_group and payload.enabled and payload.use_substream and not camera["live_substream_url_ciphertext"]:
            raise HTTPException(status_code=422, detail="Configure and validate a lower-resolution camera profile, or select the main stream")
        db.execute(
            "UPDATE ai_camera_settings SET config_override=?,enabled=?,analysis_fps=?,confidence_threshold=?,use_substream=?,zones_json=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE camera_id=?",
            (int(not payload.inherit_group), int(payload.enabled), payload.analysis_fps, payload.confidence_threshold, int(payload.use_substream), json.dumps(zones, separators=(",", ":")), camera_id),
        )
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "ai_detection_configured", "camera", camera_id,
                    json.dumps({"enabled": payload.enabled, "inherit_group": payload.inherit_group, "analysis_fps": payload.analysis_fps, "model_id": active_model["model_id"], "model_version": active_model["model_version"], "provider": active_model["provider"]}, separators=(",", ":"))))
    await request.app.state.detectors.sync_camera(camera_id, force_restart=True)
    return get_ai_detection_config(camera_id, _=user)


def _group_ai_view(group_id: str) -> dict[str, object]:
    with connect() as db:
        group = db.execute("SELECT id,name FROM camera_groups WHERE id=?", (group_id,)).fetchone()
        if group is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        config = db.execute("SELECT * FROM ai_group_settings WHERE group_id=?", (group_id,)).fetchone()
        camera_count = db.execute("SELECT COUNT(*) FROM cameras WHERE group_id=?", (group_id,)).fetchone()[0]
        inherited_count = db.execute("SELECT COUNT(*) FROM cameras c LEFT JOIN ai_camera_settings s ON s.camera_id=c.id WHERE c.group_id=? AND COALESCE(s.config_override,0)=0", (group_id,)).fetchone()[0]
    return {
        "group_id": group_id, "group_name": group["name"], "configured": config is not None,
        "enabled": bool(config["enabled"]) if config else False,
        "analysis_fps": config["analysis_fps"] if config else 0.5,
        "confidence_threshold": config["confidence_threshold"] if config else 0.55,
        "use_substream": bool(config["use_substream"]) if config else True,
        "zones": json.loads(config["zones_json"]) if config else [],
        "camera_count": camera_count, "inherited_camera_count": inherited_count,
        "model": _ai_runtime_model_view(),
    }


@router.get("/api/camera-groups/{group_id}/ai")
def get_group_ai_config(group_id: str, _: CurrentUser) -> dict[str, object]:
    return _group_ai_view(group_id)


@router.put("/api/camera-groups/{group_id}/ai")
async def update_group_ai_config(
    group_id: str,
    payload: AIDetectionConfigRequest,
    request: Request,
    user: Administrator,
) -> dict[str, object]:
    zones = [zone.model_dump() for zone in payload.zones]
    active_model = _ai_runtime_model_view()
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera group not found")
        if payload.enabled and payload.use_substream:
            missing = db.execute("SELECT COUNT(*) FROM cameras c LEFT JOIN ai_camera_settings s ON s.camera_id=c.id WHERE c.group_id=? AND c.enabled=1 AND COALESCE(s.config_override,0)=0 AND c.live_substream_url_ciphertext IS NULL", (group_id,)).fetchone()[0]
            if missing:
                raise HTTPException(status_code=422, detail=f"{missing} inherited camera(s) do not have a validated lower-resolution profile; choose the main stream or configure the profiles")
        db.execute(
            "INSERT INTO ai_group_settings(group_id,enabled,analysis_fps,confidence_threshold,use_substream,zones_json,updated_at) VALUES(?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ON CONFLICT(group_id) DO UPDATE SET enabled=excluded.enabled,analysis_fps=excluded.analysis_fps,confidence_threshold=excluded.confidence_threshold,use_substream=excluded.use_substream,zones_json=excluded.zones_json,updated_at=excluded.updated_at",
            (group_id, int(payload.enabled), payload.analysis_fps, payload.confidence_threshold, int(payload.use_substream), json.dumps(zones, separators=(",", ":"))),
        )
        camera_ids = [row[0] for row in db.execute("SELECT c.id FROM cameras c LEFT JOIN ai_camera_settings s ON s.camera_id=c.id WHERE c.group_id=? AND c.enabled=1 AND COALESCE(s.config_override,0)=0", (group_id,)).fetchall()]
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "ai_group_configured", "camera_group", group_id,
                    json.dumps({"enabled": payload.enabled, "inherited_camera_count": len(camera_ids), "analysis_fps": payload.analysis_fps, "model_id": active_model["model_id"], "model_version": active_model["model_version"], "provider": active_model["provider"]}, separators=(",", ":"))))
    for camera_id in camera_ids:
        await request.app.state.detectors.sync_camera(camera_id, force_restart=True)
    return _group_ai_view(group_id)


@router.get("/api/ai/settings")
def get_ai_runtime_settings(_: CurrentUser) -> dict[str, object]:
    with connect() as db:
        row = db.execute("SELECT max_active_cameras,updated_at FROM ai_runtime_settings WHERE singleton_id=1").fetchone()
    return {"max_active_cameras": int(row["max_active_cameras"]), "inflight_inferences": 1, "updated_at": row["updated_at"],
            "resource_note": "Inference calls run through one bounded slot to preserve headroom for recording. Cameras beyond the active limit are marked skipped; no unbounded analysis queue is maintained.",
            "model": _ai_runtime_model_view()}


@router.post("/api/ai/settings/validate")
async def validate_ai_runtime_settings(payload: AIRuntimeValidationRequest, _: Administrator) -> dict[str, object]:
    result = await asyncio.to_thread(validate_detector, payload.provider, payload.model_id)
    return {**result, "activation_changed": False, "gpu_certification_note": "GPU is certified only for this exact host, OpenVINO runtime, model, and active recording workload after validation."}


@router.put("/api/ai/settings")
async def update_ai_runtime_settings(payload: AIRuntimeSettingsRequest, request: Request, user: Administrator) -> dict[str, object]:
    with connect() as db:
        current = db.execute("SELECT provider,model_id,model_version FROM ai_runtime_settings WHERE singleton_id=1").fetchone()
    provider = payload.provider or str(current["provider"])
    model_id = payload.model_id or str(current["model_id"])
    model_version = str(current["model_version"])
    validated_at: str | None = None
    if payload.provider is not None or payload.model_id is not None:
        validation = await asyncio.to_thread(validate_detector, provider, model_id)
        if not validation["valid"]:
            raise HTTPException(status_code=422, detail=f"AI runtime was not changed: {validation['reason']}")
        model_version = str(validation["model_version"])
        validated_at = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("UPDATE ai_runtime_settings SET max_active_cameras=?,provider=?,model_id=?,model_version=?,validated_at=COALESCE(?,validated_at),updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE singleton_id=1",
                   (payload.max_active_cameras, provider, model_id, model_version, validated_at))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (int(user["id"]), "ai_runtime_configured", "ai_runtime", "local",
                    json.dumps({"max_active_cameras": payload.max_active_cameras, "provider": provider, "model_id": model_id, "model_version": model_version, "validated": validated_at is not None}, separators=(",", ":"))))
    await request.app.state.detectors.reconcile()
    return get_ai_runtime_settings(_=user)
