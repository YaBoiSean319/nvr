from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException

from app import db_health

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def health() -> dict[str, str]:
    """The container's health check. It opens the database and reads the start of every table (milliseconds), so a damaged file is reported
    as unhealthy; ``SELECT 1`` reads no page of the file and passed on a damaged one (#321). Docker only marks the container unhealthy; the
    compose restart policy does not restart on that."""
    try:
        outcome = db_health.check(full=False)
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    if not outcome.ok:
        raise HTTPException(status_code=503, detail="Database damaged")
    return {"status": "ok"}
