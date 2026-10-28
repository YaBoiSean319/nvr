"""Failover recorder (#143): the administrator's settings and status, the status for everyone, and the signed endpoints the two NVRs call on each other."""
from __future__ import annotations

import json
import time
from collections import deque
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from app import failover
from app.audit import record
from app.credentials import encrypt
from app.principal import Administrator, CurrentUser
from app.store import connect

router = APIRouter()


class FailoverInput(BaseModel):
    role: Literal["off", "primary", "standby"]
    peer_url: str | None = Field(default=None, max_length=2048)
    secret: str | None = Field(default=None, max_length=256)  # Write-only: left out, the saved one is kept
    allow_private: bool = False
    silence_seconds: int = Field(default=120, ge=10, le=3600)
    lease_seconds: int = Field(default=60, ge=5, le=1800)
    sync_seconds: int = Field(default=900, ge=60, le=86400)

    @field_validator("secret")
    @classmethod
    def long_enough(cls, value: str | None) -> str | None:
        if value is not None and len(value) < failover.MIN_SECRET_LENGTH:
            raise ValueError(f"The failover secret must be at least {failover.MIN_SECRET_LENGTH} characters")
        return value

    @model_validator(mode="after")
    def consistent(self) -> "FailoverInput":
        if self.silence_seconds < self.lease_seconds * 2:
            raise ValueError("The standby must wait at least twice the lease before it takes over, so a lease it was granted has always run out")
        return self


def _view(db) -> dict[str, Any]:
    return {"settings": failover.read_settings(db), "status": failover.status_view(db), "episodes": failover.episodes(db)}


@router.get("/api/admin/failover")
def get_failover(_: Administrator) -> dict[str, Any]:
    with connect() as db:
        return _view(db)


@router.put("/api/admin/failover")
def put_failover(body: FailoverInput, user: Administrator) -> dict[str, Any]:
    peer = None
    if body.role != "off":
        if not body.peer_url:
            raise HTTPException(status_code=422, detail="Enter the address of the other NVR")
        try:
            peer = failover.check_peer_url(body.peer_url, body.allow_private)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            current, state = failover.read_settings(db), failover.read_state(db)
            if body.role != "off" and not body.secret and not current["has_secret"]:
                raise HTTPException(status_code=422, detail=f"Enter a failover secret of at least {failover.MIN_SECRET_LENGTH} characters, the same on both NVRs")
            if current["role"] == "standby" and body.role != "standby" and (state["takeover_active"] or db.execute("SELECT 1 FROM failover_episodes WHERE role='standby' AND state='handback'").fetchone()):
                raise HTTPException(status_code=409, detail="This standby is recording in the primary's place or handing back: let the handback finish first")
            db.execute("UPDATE failover_settings SET role=?,peer_url=?,allow_private=?,silence_seconds=?,lease_seconds=?,sync_seconds=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE singleton_id=1",
                       (body.role, peer, int(body.allow_private), body.silence_seconds, body.lease_seconds, body.sync_seconds))
            if body.secret:
                db.execute("UPDATE failover_settings SET secret_ciphertext=? WHERE singleton_id=1", (encrypt(body.secret),))
            if body.role == "standby" and current["role"] != "standby":
                failover._set_state(db, watching_since=failover._stamp(), last_renewed_at=None, takeover_active=0, takeover_started_at=None, yield_until=None, last_error=None, peer_state=None)
            elif body.role != "standby":
                failover._set_state(db, takeover_active=0, yield_until=None)
            record(db, int(user["id"]), "failover_settings_changed", "failover", "1", role=body.role, peer=peer, secret_changed=bool(body.secret))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return _view(db)


@router.post("/api/admin/failover/sync", status_code=202)
def sync_now(user: Administrator) -> dict[str, Any]:
    """A standby fetches the primary's configuration now (at the manager's next pass)."""
    with connect() as db:
        if failover.read_settings(db)["role"] != "standby":
            raise HTTPException(status_code=409, detail="Only a standby copies the primary's configuration")
        failover._set_state(db, sync_requested=1)
        record(db, int(user["id"]), "failover_sync_requested", "failover", "1")
        return {"requested": True}


@router.get("/api/failover/status")
def get_status(_: CurrentUser) -> dict[str, Any]:
    """Where failover stands, for everyone who is signed in (the status area shows it)."""
    with connect() as db:
        return failover.status_view(db)


# --- the signed endpoints the two NVRs call on each other (the signature is the credential, not a sign-in) ----------------------------------------

MAX_BODY_BYTES = 4096
MAX_FAILURES_PER_MINUTE = 20
_failures: dict[str, deque[float]] = {}


def _blocked(host: str) -> bool:
    """A sender that has failed the signature too often in the last minute is refused (429) without being checked, so guessing the secret is not worth trying."""
    now = time.monotonic()
    window = _failures.setdefault(host, deque())
    while window and now - window[0] > 60:
        window.popleft()
    if len(_failures) > 1000:
        for key in [key for key, times in _failures.items() if not times or now - times[-1] > 60]:
            del _failures[key]
    return len(window) >= MAX_FAILURES_PER_MINUTE


def _target(request: Request) -> str:
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


async def _signed(request: Request) -> bytes:
    host = request.client.host if request.client else ""
    if _blocked(host):
        raise failover.PeerError(429, "Too many refused requests")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise failover.PeerError(413, "The request is too large")
    body = bytearray()
    async for chunk in request.stream():  # Capped as it is read: a chunked body declares no length, and nothing is checked yet
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise failover.PeerError(413, "The request is too large")
    body = bytes(body)
    try:
        await run_in_threadpool(failover.authenticate, request.method, _target(request), body, request.headers.get("x-failover-timestamp"), request.headers.get("x-failover-signature"))
    except failover.PeerError:
        _failures.setdefault(host, deque()).append(time.monotonic())
        raise
    return body


def _refusal(exc: failover.PeerError) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


def _epoch(request: Request) -> int:
    try:
        return int(request.query_params.get("epoch", ""))
    except ValueError:
        raise failover.PeerError(422, "An epoch is needed") from None


@router.post("/api/inbound/failover/lease")
async def lease(request: Request) -> JSONResponse:
    """The primary renews its lease. The standby's answer says whether it is granted and whether footage waits to be brought back."""
    try:
        await _signed(request)
        return JSONResponse(await run_in_threadpool(failover.standby_lease, {}))
    except failover.PeerError as exc:
        return _refusal(exc)


@router.post("/api/inbound/failover/handback-complete")
async def handback_complete(request: Request) -> JSONResponse:
    try:
        body = await _signed(request)
        try:
            payload = json.loads(body or b"{}")
            epoch, imported, size = int(payload["epoch"]), int(payload.get("imported", 0)), int(payload.get("bytes", 0))
        except (ValueError, KeyError, TypeError):
            raise failover.PeerError(422, "The request is not understood") from None
        await run_in_threadpool(failover.standby_handback_complete, epoch, imported, size)
        return JSONResponse({"done": True})
    except failover.PeerError as exc:
        return _refusal(exc)


@router.get("/api/inbound/failover/footage")
async def footage(request: Request) -> JSONResponse:
    try:
        await _signed(request)
        return JSONResponse({"segments": await run_in_threadpool(failover.standby_footage, _epoch(request))})
    except failover.PeerError as exc:
        return _refusal(exc)


@router.get("/api/inbound/failover/footage/{segment_id}")
async def footage_file(segment_id: int, request: Request):
    try:
        await _signed(request)
        path, digest = await run_in_threadpool(failover.standby_footage_file, _epoch(request), segment_id)
    except failover.PeerError as exc:
        return _refusal(exc)
    return FileResponse(path, media_type="video/mp4", headers={"X-Footage-Sha256": digest, "Cache-Control": "no-store"})


@router.get("/api/inbound/failover/backup")
async def backup_file(request: Request):
    """The primary's newest encrypted backup, for the standby to copy the configuration from (it holds the credential key, so it is sealed with the backup passphrase)."""
    try:
        await _signed(request)
        path = await run_in_threadpool(failover.backup_for_standby)
    except failover.PeerError as exc:
        return _refusal(exc)
    return FileResponse(path, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})
