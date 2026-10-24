from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException

from app.store import connect

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def health() -> dict[str, str]:
    try:
        with connect() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok"}
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
