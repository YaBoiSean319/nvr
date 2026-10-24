"""Camera discovery, CSV import and reviewed bulk changes."""
from __future__ import annotations

import asyncio
import csv
import io
import os
import secrets
import sqlite3
import uuid
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.principal import Administrator
from app.store import connect
from app.recording import probe_stream, safe_stream_url
from app.discovery import discover_onvif, discover_onvif_ip_range
from app import diagnostics
from app.request_validation import normalize_group_name
from app.api.cameras import CameraCreateRequest, CameraProbeRequest, MotionZoneRequest, camera_view, insert_camera, record_discovery_failure, record_discovery_probe, create_camera

router = APIRouter()


class CameraImportRequest(BaseModel):
    csv_text: str = Field(min_length=1, max_length=1_000_000)


class CameraImportRow(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    stream_url: str = Field(min_length=8, max_length=2048)
    recording_mode: Literal["motion", "continuous", "disabled"] = "motion"
    group_name: str | None = Field(default=None, max_length=80)
    onvif_uuid: str | None = None

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str | None) -> str | None:
        value = value.strip() if isinstance(value, str) else value
        return (value or None) if isinstance(value, str) else value

    @field_validator("group_name")
    @classmethod
    def trim_group_name(cls, value: str | None) -> str | None:
        return normalize_group_name(value)

    @field_validator("stream_url")
    @classmethod
    def trim_stream_url(cls, value: str) -> str:
        return value.strip()

    @field_validator("onvif_uuid")
    @classmethod
    def validate_onvif_uuid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ValueError("ONVIF identity must be a UUID") from exc


class CameraBulkUpdateRequest(BaseModel):
    camera_ids: list[str] = Field(min_length=1, max_length=500)
    group_name: str | None = Field(default=None, max_length=80)
    recording_mode: Literal["motion", "continuous", "disabled"] | None = None
    recording_enabled: bool | None = None

    @field_validator("group_name")
    @classmethod
    def trim_group_name(cls, value: str | None) -> str | None:
        return normalize_group_name(value)


class DiscoveryScanRequest(BaseModel):
    timeout_seconds: float = Field(default=5, ge=0.5, le=15)
    ip_range: str | None = Field(default=None, min_length=1, max_length=64)


class DiscoveredCameraAddRequest(CameraProbeRequest):
    name: str = Field(min_length=1, max_length=120)
    recording_mode: Literal["motion", "continuous", "disabled"] = "motion"
    recording_enabled: bool = False
    motion_username: str | None = Field(default=None, max_length=256)
    motion_password: str | None = Field(default=None, max_length=1024)
    motion_pre_roll_seconds: int = Field(default=10, ge=0, le=120)
    motion_post_roll_seconds: int = Field(default=30, ge=1, le=600)
    motion_fallback_enabled: bool = False
    motion_zones: list[MotionZoneRequest] = Field(default_factory=list, max_length=16)
    motion_sensitivity: int = Field(default=50, ge=1, le=100)


class DiscoveredCameraLinkRequest(BaseModel):
    camera_id: str = Field(min_length=1, max_length=64)


def _camera_import_records(csv_text: str) -> list[dict[str, str | None]]:
    try:
        reader = csv.reader(io.StringIO(csv_text.lstrip("\ufeff"), newline=""), strict=True)
        header = next(reader, None)
        if not header:
            raise HTTPException(status_code=422, detail="CSV must include a header row")
        header = [field.strip().casefold() for field in header]
        allowed = {"name", "stream_url", "recording_mode", "group_name", "onvif_uuid"}
        if len(set(header)) != len(header) or not {"name", "stream_url"}.issubset(header) or not set(header).issubset(allowed):
            raise HTTPException(status_code=422, detail="CSV headers must include name and stream_url and use only recording_mode, group_name, and onvif_uuid as optional columns")
        records: list[dict[str, str | None]] = []
        for row in reader:
            if not row or all(not value.strip() for value in row):
                continue
            values = dict(zip(header, row))
            if len(row) > len(header):
                values["__row_error__"] = "Row has a different number of columns than the header"
            records.append(values)
            if len(records) > 500:
                raise HTTPException(status_code=413, detail="A camera import may contain at most 500 rows")
        if not records:
            raise HTTPException(status_code=422, detail="CSV contains no camera rows")
        return records
    except csv.Error as exc:
        raise HTTPException(status_code=422, detail="CSV data is malformed") from exc


def _camera_import_preview(csv_text: str) -> tuple[list[dict[str, object]], list[CameraImportRow | None]]:
    records = _camera_import_records(csv_text)
    with connect() as db:
        existing = db.execute("SELECT name,onvif_uuid FROM cameras").fetchall()
    existing_names = {row["name"].casefold() for row in existing}
    existing_uuids = {row["onvif_uuid"] for row in existing if row["onvif_uuid"]}
    seen_names: set[str] = set()
    seen_uuids: set[str] = set()
    results: list[dict[str, object]] = []
    validated: list[CameraImportRow | None] = []
    for index, record in enumerate(records, start=2):
        row_errors: list[str] = []
        if record.get("__row_error__"):
            row_errors.append(str(record["__row_error__"]))
        try:
            item = CameraImportRow.model_validate(record)
            if not item.name:
                row_errors.append("Camera name is required")
            try:
                safe_stream_url(item.stream_url)
            except ValueError as exc:
                row_errors.append(str(exc))
            name_key = item.name.casefold()
            if name_key in existing_names or name_key in seen_names:
                row_errors.append("Camera name already exists or is duplicated in this import")
            if item.onvif_uuid:
                if item.onvif_uuid in existing_uuids or item.onvif_uuid in seen_uuids:
                    row_errors.append("ONVIF device identity already exists or is duplicated in this import")
            if not row_errors:
                seen_names.add(name_key)
                if item.onvif_uuid:
                    seen_uuids.add(item.onvif_uuid)
        except ValidationError as exc:
            item = None
            row_errors.extend(error["msg"] for error in exc.errors(include_input=False))
        validated.append(item if not row_errors else None)
        results.append({
            "row": index,
            "name": item.name if item else (str(record.get("name") or "")[:120]),
            "group_name": item.group_name if item else None,
            "status": "accepted" if not row_errors else ("duplicate" if any("already exists or is duplicated" in message for message in row_errors) else "invalid"),
            "errors": row_errors,
        })
    return results, validated


@router.post("/api/cameras/import/preview")
def preview_camera_import(
    payload: CameraImportRequest,
    _: Administrator,
) -> dict[str, object]:
    rows, _ = _camera_import_preview(payload.csv_text)
    return {"rows": rows, "accepted_count": sum(row["status"] == "accepted" for row in rows), "rejected_count": sum(row["status"] != "accepted" for row in rows)}


@router.post("/api/cameras/import/apply")
async def apply_camera_import(
    payload: CameraImportRequest,
    request: Request,
    _: Administrator,
) -> dict[str, object]:
    preview_rows, validated = _camera_import_preview(payload.csv_text)
    probe_limit = asyncio.Semaphore(8)

    async def check(item: CameraImportRow | None) -> dict[str, object] | None:
        if item is None:
            return None
        async with probe_limit:
            return await probe_stream(item.stream_url)

    checks = await asyncio.gather(*(check(item) for item in validated))
    results: list[dict[str, object]] = []
    pending: list[tuple[int, CameraImportRow, dict[str, object]]] = []
    for index, (preview, item, capability) in enumerate(zip(preview_rows, validated, checks)):
        result = dict(preview)
        if item is None:
            results.append(result)
        elif not capability or not capability.get("reachable"):
            result["status"] = "failed"
            result["errors"] = [str((capability or {}).get("reason", "Camera stream could not be opened"))]
            results.append(result)
        else:
            pending.append((index, item, capability))
            results.append(result)

    inserted: list[tuple[int, str, str]] = []
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for index, item, capability in pending:
                group_id = None
                if item.group_name:
                    found = db.execute("SELECT id FROM camera_groups WHERE name=? COLLATE NOCASE", (item.group_name,)).fetchone()
                    if found:
                        group_id = found["id"]
                    else:
                        group_id = str(uuid.uuid4())
                        db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)", (group_id, item.group_name))
                mode = item.recording_mode
                try:
                    camera_id = insert_camera(
                        db, name=item.name, stream_url=item.stream_url, recording_mode=mode,
                        capability=capability, onvif_uuid=item.onvif_uuid, group_id=group_id,
                    )
                    record_discovery_probe(db, item.onvif_uuid, capability)
                    if item.onvif_uuid:
                        db.execute("UPDATE discovery_candidates SET linked_camera_id=?,reviewed_at=? WHERE device_uuid=?",(camera_id,datetime.now(timezone.utc).isoformat(),item.onvif_uuid))
                    inserted.append((index,camera_id,mode))
                except sqlite3.IntegrityError:
                    results[index]["status"] = "duplicate"
                    results[index]["errors"] = ["Camera name or ONVIF identity now exists"]
            db.commit()
        except Exception:
            db.rollback()
            raise
    for index,camera_id,mode in inserted:
        await request.app.state.recorders.sync_camera(camera_id)
        results[index]["status"] = "created"
    return {"rows": results, "created_count": len(inserted), "failed_count": sum(row["status"] not in {"created","duplicate"} for row in results)}


def _candidate_view(row: sqlite3.Row) -> dict[str, object]:
    claimed_until = row["add_claimed_until"]
    add_in_progress = False
    if row["add_claim_token"] and claimed_until:
        try:
            add_in_progress = datetime.fromisoformat(claimed_until.replace("Z", "+00:00")) > datetime.now(timezone.utc)
        except ValueError:
            add_in_progress = False
    return {
        "id": row["id"],
        "device_uuid": row["device_uuid"],
        "service_urls": json.loads(row["service_urls"]),
        "scopes": json.loads(row["scopes"]),
        "capabilities": json.loads(row["capabilities"]),
        "linked_camera_id": row["linked_camera_id"],
        "add_in_progress": add_in_progress,
        "reviewed_at": row["reviewed_at"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
    }


def _claim_discovery_add(candidate_id: str) -> tuple[sqlite3.Row, str]:
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    claimed_until = (now + timedelta(minutes=5)).isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        candidate = db.execute("SELECT * FROM discovery_candidates WHERE id=?", (candidate_id,)).fetchone()
        if candidate is None:
            db.rollback()
            raise HTTPException(status_code=404, detail="Discovery candidate not found")
        if candidate["linked_camera_id"]:
            db.rollback()
            raise HTTPException(status_code=409, detail="This discovered device is already linked to a camera")
        if candidate["add_claim_token"] and candidate["add_claimed_until"]:
            try:
                another_add_is_active = datetime.fromisoformat(
                    candidate["add_claimed_until"].replace("Z", "+00:00")
                ) > now
            except ValueError:
                another_add_is_active = False
            if another_add_is_active:
                db.rollback()
                raise HTTPException(status_code=409, detail="This discovered device is already being added")
        db.execute(
            "UPDATE discovery_candidates SET add_claim_token=?,add_claimed_until=? WHERE id=? AND linked_camera_id IS NULL",
            (token, claimed_until, candidate_id),
        )
        db.commit()
        claimed = db.execute("SELECT * FROM discovery_candidates WHERE id=?", (candidate_id,)).fetchone()
    return claimed, token


def _release_discovery_add(candidate_id: str, token: str) -> None:
    with connect() as db:
        db.execute(
            "UPDATE discovery_candidates SET add_claim_token=NULL,add_claimed_until=NULL "
            "WHERE id=? AND add_claim_token=? AND linked_camera_id IS NULL",
            (candidate_id, token),
        )


@router.post("/api/discovery/scan")
async def scan_cameras(
    payload: DiscoveryScanRequest,
    _: Administrator,
) -> dict[str, object]:
    started = time.monotonic()
    interface_configured = bool(os.environ.get("NVR_DISCOVERY_INTERFACE", "").strip())
    scan_mode = "ip_range" if payload.ip_range else "multicast"
    try:
        if payload.ip_range:
            result = await discover_onvif_ip_range(payload.ip_range)
        else:
            result = await discover_onvif(
                payload.timeout_seconds,
                os.environ.get("NVR_DISCOVERY_INTERFACE", "").strip() or None,
            )
    except ValueError as exc:
        diagnostics.record_event(
            "discovery_scan", outcome="failed", elapsed_ms=min(60000, int((time.monotonic() - started) * 1000)),
            interface_configured=interface_configured,
            error_category="configuration",
            scan_mode=scan_mode,
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        diagnostics.record_event(
            "discovery_scan", outcome="failed", elapsed_ms=min(60000, int((time.monotonic() - started) * 1000)),
            interface_configured=interface_configured, error_category="network", scan_mode=scan_mode,
        )
        raise HTTPException(status_code=503, detail="Camera discovery failed; check the NVR discovery network configuration") from exc

    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        for device in result.devices:
            linked = db.execute("SELECT id FROM cameras WHERE onvif_uuid=?", (device.device_uuid,)).fetchone()
            existing = db.execute("SELECT id,capabilities FROM discovery_candidates WHERE device_uuid=?", (device.device_uuid,)).fetchone()
            candidate_id = existing["id"] if existing else str(uuid.uuid4())
            capabilities = {
                "onvif_discovery": "supported",
                "video_stream": "untested",
                "motion_events": "untested",
                "audio": "untested",
                "ptz": "untested",
            }
            if existing:
                capabilities.update(json.loads(existing["capabilities"]))
            db.execute(
                "INSERT INTO discovery_candidates(id,device_uuid,service_urls,scopes,capabilities,linked_camera_id,first_seen_at,last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(device_uuid) DO UPDATE SET "
                "service_urls=excluded.service_urls,scopes=excluded.scopes,capabilities=excluded.capabilities,"
                "linked_camera_id=COALESCE(excluded.linked_camera_id,discovery_candidates.linked_camera_id),last_seen_at=excluded.last_seen_at",
                (candidate_id, device.device_uuid, json.dumps(device.service_urls), json.dumps(device.scopes),
                 json.dumps(capabilities), linked["id"] if linked else None, now, now),
            )
        rows = db.execute("SELECT * FROM discovery_candidates ORDER BY last_seen_at DESC,device_uuid").fetchall()
    partial_failures = [f"Ignored {result.ignored_responses} malformed or unrecognized response(s)"] if result.ignored_responses else []
    diagnostics.record_event(
        "discovery_scan", outcome="completed", elapsed_ms=min(60000, int((time.monotonic() - started) * 1000)),
        interface_configured=interface_configured, discovered_count=len(result.devices), ignored_responses=result.ignored_responses,
        scan_mode=scan_mode, targets_scanned=result.targets_scanned,
    )
    return {
        "scan_mode": scan_mode,
        "targets_scanned": result.targets_scanned,
        "discovered_count": len(result.devices),
        "ignored_responses": result.ignored_responses,
        "partial_failures": partial_failures,
        "candidates": [_candidate_view(row) for row in rows],
    }


@router.get("/api/discovery/candidates")
def list_discovery_candidates(_: Administrator) -> dict[str, object]:
    with connect() as db:
        rows = db.execute("SELECT * FROM discovery_candidates ORDER BY last_seen_at DESC,device_uuid").fetchall()
    return {"candidates": [_candidate_view(row) for row in rows]}


@router.post("/api/discovery/candidates/{candidate_id}/link")
def link_discovered_camera(
    candidate_id: str,
    payload: DiscoveredCameraLinkRequest,
    _: Administrator,
) -> dict[str, object]:
    with connect() as db:
        candidate = db.execute("SELECT device_uuid,linked_camera_id FROM discovery_candidates WHERE id=?", (candidate_id,)).fetchone()
        if candidate is None:
            raise HTTPException(status_code=404, detail="Discovery candidate not found")
        if candidate["linked_camera_id"] and candidate["linked_camera_id"] != payload.camera_id:
            raise HTTPException(status_code=409, detail="This discovered device is already linked to another camera")
        cursor = db.execute("SELECT id FROM cameras WHERE id=?", (payload.camera_id,)).fetchone()
        if cursor is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if not candidate["device_uuid"].startswith("ipscan:"):
            try:
                db.execute("UPDATE cameras SET onvif_uuid=?,updated_at=? WHERE id=?", (candidate["device_uuid"], datetime.now(timezone.utc).isoformat(), payload.camera_id))
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="This ONVIF identity is already assigned to another camera") from exc
        db.execute("UPDATE discovery_candidates SET linked_camera_id=?,reviewed_at=? WHERE id=?", (payload.camera_id, datetime.now(timezone.utc).isoformat(), candidate_id))
        row = db.execute("SELECT id,name,recording_mode FROM cameras WHERE id=?", (payload.camera_id,)).fetchone()
    return {"camera": dict(row), "device_uuid": candidate["device_uuid"]}


@router.post("/api/discovery/candidates/{candidate_id}/add", status_code=status.HTTP_201_CREATED)
async def add_discovered_camera(
    candidate_id: str,
    payload: DiscoveredCameraAddRequest,
    request: Request,
    user: Administrator,
) -> dict[str, object]:
    candidate, claim_token = _claim_discovery_add(candidate_id)
    request.state.discovery_add_claim = {
        "candidate_id": candidate_id,
        "device_uuid": candidate["device_uuid"],
        "token": claim_token,
    }
    try:
        camera = await create_camera(
            CameraCreateRequest(
                name=payload.name,
                stream_url=payload.stream_url,
                camera_ip=payload.camera_ip,
                camera_username=payload.camera_username,
                camera_password=payload.camera_password,
                camera_brand=payload.camera_brand,
                onvif_profile_token=payload.onvif_profile_token,
                recording_mode=payload.recording_mode,
                recording_enabled=payload.recording_enabled,
                motion_service_url=payload.device_service_url or (json.loads(candidate["service_urls"])[0] if candidate["service_urls"] else None),
                motion_username=payload.motion_username or payload.camera_username,
                motion_password=payload.motion_password or payload.camera_password,
                motion_pre_roll_seconds=payload.motion_pre_roll_seconds,
                motion_post_roll_seconds=payload.motion_post_roll_seconds,
                motion_fallback_enabled=payload.motion_fallback_enabled,
                motion_zones=payload.motion_zones,
                motion_sensitivity=payload.motion_sensitivity,
                onvif_uuid=None if candidate["device_uuid"].startswith("ipscan:") else candidate["device_uuid"],
            ),
            request,
            user,
        )
    except HTTPException as exc:
        if exc.status_code == 422:
            with connect() as db:
                record_discovery_failure(db, candidate["device_uuid"])
        _release_discovery_add(candidate_id, claim_token)
        raise
    except Exception:
        _release_discovery_add(candidate_id, claim_token)
        raise
    return camera


@router.post("/api/cameras/bulk-update")
async def bulk_update_cameras(
    payload: CameraBulkUpdateRequest,
    request: Request,
    _: Administrator,
) -> dict[str, object]:
    if "group_name" not in payload.model_fields_set and payload.recording_mode is None and payload.recording_enabled is None:
        raise HTTPException(status_code=422, detail="Specify a group assignment, recording mode, or enablement")
    if len(set(payload.camera_ids)) != len(payload.camera_ids):
        raise HTTPException(status_code=422, detail="Camera IDs must be unique")
    results: list[dict[str, object]] = []
    updated: list[dict[str, object]] = []
    for camera_id in payload.camera_ids:
        try:
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT id,archived_at,recording_mode FROM cameras WHERE id=?", (camera_id,)).fetchone()
                if current is None:
                    results.append({"camera_id": camera_id, "status": "failed", "code": "missing", "detail": "Camera is no longer available."})
                    continue
                if current["archived_at"]:
                    results.append({"camera_id": camera_id, "status": "failed", "code": "archived", "detail": "Archived cameras are excluded from bulk changes."})
                    continue

                group_id = None
                if "group_name" in payload.model_fields_set and payload.group_name:
                    group = db.execute("SELECT id FROM camera_groups WHERE name=? COLLATE NOCASE", (payload.group_name,)).fetchone()
                    if group:
                        group_id = group["id"]
                    else:
                        group_id = str(uuid.uuid4())
                        db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)", (group_id, payload.group_name))

                assignments = []
                values: list[object] = []
                if "group_name" in payload.model_fields_set:
                    assignments.append("group_id=?")
                    values.append(group_id)
                if payload.recording_mode is not None:
                    assignments.append("recording_mode=?")
                    values.append(payload.recording_mode)
                    if payload.recording_mode == "disabled":
                        assignments.extend(("recording_enabled=0", "recording_status='stopped'", "last_error=NULL"))
                enablement_blocked_by_mode = False
                if payload.recording_enabled is not None:
                    assignments.append("recording_enabled=?")
                    effective_mode = payload.recording_mode or current["recording_mode"]
                    effective_enabled = int(payload.recording_enabled and effective_mode != "disabled")
                    enablement_blocked_by_mode = payload.recording_enabled and effective_mode == "disabled"
                    values.append(effective_enabled)
                    assignments.extend(("recording_status=CASE WHEN ?=1 THEN 'connecting' ELSE 'stopped' END", "last_error=CASE WHEN ?=1 AND recording_mode='motion' AND motion_service_url IS NULL AND motion_fallback_enabled=0 THEN 'Configure an ONVIF event service or enable server motion fallback' ELSE NULL END"))
                    values.extend((effective_enabled, effective_enabled))
                elif payload.recording_mode is not None and payload.recording_mode != "disabled":
                    assignments.extend(("recording_status=CASE WHEN recording_enabled=1 THEN 'connecting' ELSE 'stopped' END", "last_error=NULL"))
                assignments.append("updated_at=?")
                values.extend((datetime.now(timezone.utc).isoformat(), camera_id))
                changed = db.execute(
                    f"UPDATE cameras SET {','.join(assignments)} WHERE id=? AND archived_at IS NULL",
                    values,
                )
                if changed.rowcount != 1:
                    results.append({"camera_id": camera_id, "status": "failed", "code": "unavailable", "detail": "Camera became unavailable before the update."})
                    continue
        except Exception:
            # A failed commit can leave the caller unable to know whether SQLite committed.
            results.append({"camera_id": camera_id, "status": "unknown", "code": "outcome_unknown", "detail": "The server could not confirm whether this camera's changes were saved."})
            continue

        warnings = []
        if enablement_blocked_by_mode:
            warnings.append("Recording remains disabled because this camera's recording mode is disabled.")
        try:
            await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
        except Exception:
            # Configuration is committed. Do not invite a retry that could duplicate side effects;
            # make the operational warning visible while reporting the durable update as successful.
            warnings.append("Settings were saved, but the recorder could not be refreshed. Check camera status.")
        with connect() as db:
            row = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
        if row is None:
            results.append({"camera_id": camera_id, "status": "unknown", "code": "outcome_unknown", "detail": "Settings were saved, but the camera is no longer available."})
            continue
        updated.append(camera_view(row))
        result: dict[str, object] = {"camera_id": camera_id, "status": "success", "code": None}
        if warnings:
            result["warning"] = " ".join(warnings)
        results.append(result)

    updated.sort(key=lambda camera: (str(camera["name"]).casefold(), str(camera["id"])))
    return {"cameras": updated, "results": results}
