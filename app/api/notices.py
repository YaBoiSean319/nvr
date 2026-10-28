"""Retention purposes, the record of automatic deletions, and the generated camera notice and data-protection summary (#139). Administrators only."""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app import notices
from app.principal import Administrator
from app.store import connect

router = APIRouter()


class SettingsBody(BaseModel):
    require_purpose: bool | None = None
    default_purpose: str | None = Field(default=None, max_length=notices.MAX_TEXT)
    default_legal_basis: str | None = Field(default=None, max_length=notices.MAX_TEXT)
    organisation: str | None = Field(default=None, max_length=notices.MAX_TEXT)
    contact_name: str | None = Field(default=None, max_length=notices.MAX_TEXT)
    contact_email: str | None = Field(default=None, max_length=notices.MAX_TEXT)
    contact_phone: str | None = Field(default=None, max_length=notices.MAX_TEXT)


def _day(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        raise HTTPException(status_code=422, detail="Enter a date such as 2026-03-01") from None


@router.get("/api/privacy/settings")
def get_settings(_: Administrator) -> dict[str, object]:
    with connect() as db:
        return {**notices.settings(db), "policies": notices.policies(db)}


@router.put("/api/privacy/settings")
def put_settings(body: SettingsBody, user: Administrator) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            notices.save_settings(db, body.model_dump(exclude_none=True))
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                       (int(user["id"]), "privacy_settings_updated", "privacy_settings", "1", json.dumps({"require_purpose": body.require_purpose}, separators=(",", ":"))))
        except notices.NoticeError as exc:
            db.execute("ROLLBACK")
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return {**notices.settings(db), "policies": notices.policies(db)}


@router.get("/api/retention-deletions")
def get_deletions(_: Administrator, camera_id: str | None = Query(default=None, max_length=64), start: str | None = Query(default=None, max_length=40),
                  end: str | None = Query(default=None, max_length=40), limit: int = Query(default=500, ge=1, le=5000)) -> dict[str, object]:
    """What automatic retention removed, per camera and day, with the policy and purpose in force when it did."""
    with connect() as db:
        records = notices.deletions(db, camera_id=camera_id, start=_day(start), end=_day(end), limit=limit)
    return {"deletions": records, "totals": {"segments": sum(item["segments"] for item in records), "bytes": sum(item["bytes"] for item in records)}}


@router.get("/api/privacy/notice", response_model=None)
def get_notice(_: Administrator, format: str = Query(default="json", pattern="^(json|html)$")):
    with connect() as db:
        data = notices.notice(db)
    return HTMLResponse(notices.render_notice(data)) if format == "html" else data


@router.get("/api/privacy/summary", response_model=None)
def get_summary(_: Administrator, format: str = Query(default="json", pattern="^(json|html)$")):
    with connect() as db:
        data = notices.summary(db)
    return HTMLResponse(notices.render_summary(data)) if format == "html" else data
