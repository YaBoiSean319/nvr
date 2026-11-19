"""The security log for administrators: the page's rows, a CSV, and how long it is kept (#367)."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import security_log
from app.api.viewing_audit import _cell, _range
from app.audit import record
from app.clock import stamp
from app.principal import Administrator
from app.store import connect

router = APIRouter()

MAX_CSV_ROWS = 100_000
Action = Annotated[str | None, Query(max_length=64, pattern=r"^[a-z0-9_]+$")]
From = Annotated[datetime | None, Query(alias="from")]
To = Annotated[datetime | None, Query(alias="to")]


@router.get("/api/admin/security-log")
def security_log_entries(_: Administrator, start: From = None, end: To = None, user_id: int | None = None, action: Action = None,
                         limit: Annotated[int, Query(ge=1, le=500)] = 100, offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, object]:
    start, end = _range(start, end)
    with connect() as db:
        return {**security_log.entries(db, start, end, user_id, action, limit, offset), "actions": security_log.actions(db)}


@router.get("/api/admin/security-log/export.csv")
def security_log_csv(admin: Administrator, start: From = None, end: To = None, user_id: int | None = None, action: Action = None) -> Response:
    start, end = _range(start, end)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    with connect() as db:
        data = security_log.entries(db, start, end, user_id, action, MAX_CSV_ROWS, 0)
        if data["total"] > MAX_CSV_ROWS:
            raise HTTPException(status_code=422, detail=f"That range has {data['total']} entries, more than the {MAX_CSV_ROWS} one file holds; choose a shorter range or a filter")
        writer.writerow(["at", "person", "action", "resource_type", "resource_id", "details"])
        for row in data["entries"]:
            writer.writerow([_cell(value) for value in (row["at"], row["actor"] or "", row["action"], row["resource_type"], row["resource_id"], json.dumps(row["details"], sort_keys=True))])
        record(db, admin.id, "security_log_exported", "audit_log", "csv", start=stamp(start), end=stamp(end), rows=len(data["entries"]))
    filename = f"security-log-{start:%Y%m%d}-{end:%Y%m%d}.csv"
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


class LogSettings(BaseModel):
    retention_days: int = Field(ge=security_log.RETENTION_DAYS[0], le=security_log.RETENTION_DAYS[1])


def _view(days: int, **extra: int) -> dict[str, int]:
    return {"retention_days": days, "minimum": security_log.RETENTION_DAYS[0], "maximum": security_log.RETENTION_DAYS[1], **extra}


@router.get("/api/admin/security-log/settings")
def security_log_settings(_: Administrator) -> dict[str, int]:
    with connect() as db:
        return _view(security_log.retention_days(db))


@router.put("/api/admin/security-log/settings")
def update_security_log_settings(payload: LogSettings, admin: Administrator) -> dict[str, int]:
    with connect() as db:
        before = security_log.retention_days(db)
        db.execute("UPDATE security_settings SET security_log_retention_days=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE singleton_id=1", (payload.retention_days,))
        removed = security_log.prune(db)
        # Written after the pass, so the change that shortened the log is itself the first thing in what is left.
        record(db, admin.id, "security_log_retention_changed", "security_settings", 1, before=before, after=payload.retention_days, removed=removed)
    return _view(payload.retention_days, removed=removed)
