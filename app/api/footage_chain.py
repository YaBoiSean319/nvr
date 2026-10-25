"""Signed footage: verify a camera's recordings against its signed hash chain."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

from app import footage_chain
from app.principal import CurrentUser
from app.store import connect

router = APIRouter()

MAX_VERIFY_WINDOW = timedelta(days=7)
# Verifying re-reads every file in the range, so one runs at a time.
_verifying = threading.Lock()


@router.get("/api/cameras/{camera_id}/footage/verify")
def verify_footage(camera_id: str, _: CurrentUser, start: datetime = Query(), end: datetime = Query()) -> dict[str, object]:
    """Altered, missing or out-of-order segments in [start, end); removals the NVR recorded are expected."""
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(status_code=422, detail="Times must include a timezone")
    if not start < end or end - start > MAX_VERIFY_WINDOW:
        raise HTTPException(status_code=422, detail="Verify up to 7 days at a time")
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
    if not _verifying.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Another footage verification is running; try again when it finishes")
    try:
        return footage_chain.verify(camera_id, start, end)
    finally:
        _verifying.release()


@router.get("/api/footage/signing-key")
def signing_key(_: CurrentUser) -> dict[str, str]:
    """The installation's public key, for checking evidence packages elsewhere."""
    return {"algorithm": "Ed25519", "public_key": footage_chain.public_key()}
