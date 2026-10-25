"""Alarm-driven Live layouts and video wall sign-in."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from app import live_alarms
from app.api_tokens import TokenRejected, authenticate, token_scopes
from app.principal import WALL_COOKIE_NAME, WALL_SCOPES, Administrator, CurrentUser, secure_cookie, wall_principal
from app.security import token_digest
from app.store import connect

router = APIRouter()

WALL_SESSION_DAYS = 30


class NeighboursRequest(BaseModel):
    camera_ids: list[str] = Field(max_length=100)


class WallSignIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)


@router.get("/api/cameras/{camera_id}/neighbours")
def get_neighbours(camera_id: str, _: CurrentUser) -> dict[str, object]:
    with connect() as db:
        if not live_alarms.camera_exists(db, camera_id):
            raise HTTPException(status_code=404, detail="Camera not found")
        return {"camera_ids": live_alarms.neighbours(db, camera_id), "max": live_alarms.MAX_NEIGHBOURS}


@router.put("/api/cameras/{camera_id}/neighbours")
def put_neighbours(camera_id: str, payload: NeighboursRequest, user: Administrator) -> dict[str, object]:
    try:
        return {"camera_ids": live_alarms.set_neighbours(camera_id, payload.camera_ids, int(user["id"])), "max": live_alarms.MAX_NEIGHBOURS}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except live_alarms.NeighbourError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/live/alarm-pop-ins")
def get_pop_ins(user: CurrentUser, after: str | None = Query(default=None, max_length=200)) -> dict[str, object]:
    """New alarms that pop into Live, after the cursor this returned last time."""
    try:
        return live_alarms.pop_ins(int(user["id"]), after)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/wall/session")
def wall_sign_in(payload: WallSignIn, response: Response) -> dict[str, object]:
    """Sign a video wall in with an API token limited to read and media."""
    try:
        row = authenticate(payload.token.strip())
    except TokenRejected as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if token_scopes(row) != WALL_SCOPES:
        raise HTTPException(status_code=403, detail="A video wall needs an API token with exactly the read and media scopes")
    raw = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=WALL_SESSION_DAYS)
    with connect() as db:
        db.execute("DELETE FROM wall_sessions WHERE expires_at<=?", (now.isoformat(),))
        db.execute("INSERT INTO wall_sessions(token_hash,api_token_id,expires_at) VALUES(?,?,?)", (token_digest(raw), row["id"], expires.isoformat()))
    response.set_cookie(WALL_COOKIE_NAME, raw, httponly=True, secure=secure_cookie(), samesite="strict", path="/", max_age=WALL_SESSION_DAYS * 86400)
    return _wall_view(row["name"])


@router.get("/api/wall/session")
def wall_session(request: Request) -> dict[str, object]:
    raw = request.cookies.get(WALL_COOKIE_NAME)
    if not raw:
        raise HTTPException(status_code=401, detail="Sign the video wall in with an API token")
    principal = wall_principal(request, raw)
    with connect() as db:
        name = db.execute("SELECT name FROM api_tokens WHERE id=?", (principal.token_id,)).fetchone()["name"]
    return _wall_view(name)


@router.delete("/api/wall/session", status_code=status.HTTP_204_NO_CONTENT)
def wall_sign_out(request: Request, response: Response) -> Response:
    raw = request.cookies.get(WALL_COOKIE_NAME)
    if raw:
        with connect() as db:
            db.execute("DELETE FROM wall_sessions WHERE token_hash=?", (token_digest(raw),))
    response.delete_cookie(WALL_COOKIE_NAME, path="/", secure=secure_cookie(), httponly=True, samesite="strict")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response



def _wall_view(name: str) -> dict[str, object]:
    return {"name": name, "scopes": ["read", "media"]}
