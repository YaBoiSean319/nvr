"""The viewing audit report for administrators: JSON, CSV, and how long the audit is kept."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import viewing_audit
from app.audit import record
from app.principal import Administrator
from app.store import connect

router = APIRouter()

DEFAULT_RANGE = timedelta(days=7)
MAX_CSV_ROWS = 100_000


def _range(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    for value in (start, end):
        if value is not None and value.tzinfo is None:
            raise HTTPException(status_code=422, detail="Times must include a time zone")
    end = end or datetime.now(timezone.utc)
    start = start or end - DEFAULT_RANGE
    if start >= end:
        raise HTTPException(status_code=422, detail="The range must end after it starts")
    return start, end




@router.get("/api/admin/viewing-audit/report")
def viewing_report(_: Administrator, by: Literal["person", "camera"] = "person", start: Annotated[datetime | None, Query(alias="from")] = None,
                   end: Annotated[datetime | None, Query(alias="to")] = None, user_id: int | None = None, camera_id: str | None = Query(default=None, max_length=64)) -> dict[str, object]:
    start, end = _range(start, end)
    with connect() as db:
        return viewing_audit.report(db, start, end, by, user_id, camera_id)


@router.get("/api/admin/viewing-audit/entries")
def viewing_entries(_: Administrator, start: Annotated[datetime | None, Query(alias="from")] = None, end: Annotated[datetime | None, Query(alias="to")] = None,
                    user_id: int | None = None, camera_id: str | None = Query(default=None, max_length=64),
                    kind: Literal["live", "playback", "export", "download", "snapshot"] | None = None,
                    limit: Annotated[int, Query(ge=1, le=500)] = 100, offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, object]:
    start, end = _range(start, end)
    with connect() as db:
        return viewing_audit.entries(db, start, end, user_id, camera_id, kind, limit, offset)


def _cell(value: object) -> object:
    """A spreadsheet runs a cell that starts with these as a formula; names are free text, so neutralise them."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@router.get("/api/admin/viewing-audit/export.csv")
def viewing_csv(admin: Administrator, view: Literal["person", "camera", "entries"] = "person", start: Annotated[datetime | None, Query(alias="from")] = None,
                end: Annotated[datetime | None, Query(alias="to")] = None, user_id: int | None = None, camera_id: str | None = Query(default=None, max_length=64)) -> Response:
    start, end = _range(start, end)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    with connect() as db:
        if view == "entries":
            data = viewing_audit.entries(db, start, end, user_id, camera_id, None, MAX_CSV_ROWS, 0)
            writer.writerow(["person", "camera", "kind", "started_at", "ended_at", "seconds", "quality", "reference"])
            for row in data["entries"]:
                writer.writerow([_cell(value) for value in (row["username"], row["camera_name"], row["kind"], row["started_at"], row["ended_at"] or "", row["seconds"], row["quality"] or "", row["reference"] or "")])
            count = len(data["entries"])
        else:
            data = viewing_audit.report(db, start, end, view, user_id, camera_id)
            name, other = ("person", "cameras") if view == "person" else ("camera", "people")
            writer.writerow([name, other, "live_sessions", "live_seconds", "playback_sessions", "playback_seconds", "exports", "downloads", "snapshots", "first_at", "last_at"])
            for row in data["rows"]:
                writer.writerow([_cell(value) for value in (row["username"] if view == "person" else row["camera_name"], row[other], row["live_sessions"], row["live_seconds"],
                                                           row["playback_sessions"], row["playback_seconds"], row["exports"], row["downloads"], row["snapshots"], row["first_at"], row["last_at"])])
            count = len(data["rows"])
        record(db, admin.id, "viewing_audit_exported", "viewing_audit", view, start=viewing_audit.stamp(start), end=viewing_audit.stamp(end), rows=count)
    filename = f"viewing-audit-{view}-{start:%Y%m%d}-{end:%Y%m%d}.csv"
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


class AuditSettings(BaseModel):
    retention_days: int = Field(ge=viewing_audit.RETENTION_DAYS[0], le=viewing_audit.RETENTION_DAYS[1])


@router.get("/api/admin/viewing-audit/settings")
def audit_settings(_: Administrator) -> dict[str, int]:
    with connect() as db:
        return {"retention_days": viewing_audit.retention_days(db), "minimum": viewing_audit.RETENTION_DAYS[0], "maximum": viewing_audit.RETENTION_DAYS[1]}


@router.put("/api/admin/viewing-audit/settings")
def update_audit_settings(payload: AuditSettings, admin: Administrator) -> dict[str, int]:
    with connect() as db:
        before = viewing_audit.retention_days(db)
        db.execute("UPDATE security_settings SET audit_retention_days=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE singleton_id=1", (payload.retention_days,))
        record(db, admin.id, "viewing_audit_retention_changed", "security_settings", 1, before=before, after=payload.retention_days)
        removed = viewing_audit.prune(db)
    return {"retention_days": payload.retention_days, "removed": removed, "minimum": viewing_audit.RETENTION_DAYS[0], "maximum": viewing_audit.RETENTION_DAYS[1]}
