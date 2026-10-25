"""Camera network hardening: the report, the camera networks, running the checks, and acknowledging findings."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import hardening
from app.audit import record
from app.principal import Administrator
from app.store import connect

router = APIRouter()


class Settings(BaseModel):
    camera_networks: list[str] = Field(max_length=hardening.MAX_NETWORKS)
    probes_enabled: bool = True


class Acknowledgement(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


def _known(check: str) -> None:
    if check not in hardening.CHECKS:
        raise HTTPException(status_code=404, detail="Finding not found")


@router.get("/api/hardening")
def get_report(_: Administrator) -> dict[str, object]:
    with connect() as db:
        return hardening.report(db)


@router.post("/api/hardening/run")
async def run_checks(admin: Administrator) -> dict[str, object]:
    """Run every check now. A camera probed within the probe gap is not probed again: its last answer is used."""
    result = await asyncio.to_thread(hardening.run, "manual")
    with connect() as db:
        record(db, admin.id, "hardening_run", "hardening", "checks", high=result["counts"]["high"], medium=result["counts"]["medium"])
    return result


@router.put("/api/hardening/settings")
async def put_settings(payload: Settings, admin: Administrator) -> dict[str, object]:
    try:
        hardening.parse_networks(payload.camera_networks)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    with connect() as db:
        before = [str(network) for network in hardening.networks(db)]
        after = hardening.set_networks(db, payload.camera_networks)
        record(db, admin.id, "hardening_networks_set", "hardening", "networks", before=before, after=after)
        if payload.probes_enabled != hardening.probes_enabled(db):
            hardening.set_probes_enabled(db, payload.probes_enabled)
            record(db, admin.id, "hardening_probes_set", "hardening", "probes", enabled=payload.probes_enabled)
    return await asyncio.to_thread(hardening.run, "manual")


@router.put("/api/hardening/findings/{camera_id}/{check}/acknowledgement")
def acknowledge(camera_id: str, check: str, payload: Acknowledgement, admin: Administrator) -> dict[str, object]:
    _known(check)
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="Say why this finding is accepted")
    with connect() as db:
        if not hardening.acknowledge(db, camera_id, check, reason, admin.id):
            raise HTTPException(status_code=404, detail="Finding not found")
        record(db, admin.id, "hardening_finding_acknowledged", "camera", camera_id, check=check, reason=reason)
        return hardening.report(db)


@router.delete("/api/hardening/findings/{camera_id}/{check}/acknowledgement")
def restore(camera_id: str, check: str, admin: Administrator) -> dict[str, object]:
    _known(check)
    with connect() as db:
        if not hardening.restore(db, camera_id, check):
            raise HTTPException(status_code=404, detail="No acknowledged finding")
        record(db, admin.id, "hardening_finding_restored", "camera", camera_id, check=check)
        return hardening.report(db)
