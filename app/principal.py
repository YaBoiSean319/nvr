"""Who is calling, and what they may do.

Every route declares its requirement through one of these dependencies. A principal
comes from a browser session cookie or from a scoped API token sent as
``Authorization: Bearer``; routes do not care which. A token acts for the
administrator who created it and only within its scopes:

- an endpoint marked with ``requires_scope`` needs that scope;
- an administrator-only route needs ``admin``;
- any other read (GET/HEAD) needs ``read``;
- any other change needs ``admin`` (operator workspace changes are for people).

``admin`` implies every other scope. Tokens are never read from query strings or
cookies. A video wall signs in once with a ``read`` and ``media`` token and then holds
a wall session cookie, which carries that token's scopes and ends with the token.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Callable, Literal, TypeVar

from fastapi import Depends, HTTPException, status
from starlette.requests import HTTPConnection
from fastapi.dependencies.models import Dependant

from .api_tokens import TokenRejected, authenticate, grants, token_scopes, usable_token
from .security import token_digest
from .store import connect

COOKIE_NAME = "nvr_session"
WALL_COOKIE_NAME = "nvr_wall"
WALL_SCOPES = frozenset({"read", "media"})
# A wall acts as its token's administrator, so it may reach only what watching needs: never
# that person's preferences, workspace or anything else a read token could otherwise see.
WALL_ROUTES = frozenset({
    "/api/wall/session",
    "/api/auth/me",  # Answers 401 for a wall, so the main interface asks for a person to sign in.
    "/api/cameras",
    "/api/cameras/{camera_id}/live",
    "/api/cameras/{camera_id}/live/status",
    "/api/cameras/{camera_id}/live/stream",
    "/api/live/views",
    "/api/live/alarm-pop-ins",
    "/api/settings/display",
})
Role = Literal["administrator", "operator"]
Scope = Literal["read", "media", "export", "admin"]
Endpoint = TypeVar("Endpoint", bound=Callable[..., object])


def secure_cookie() -> bool:
    if os.environ.get("NVR_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}:
        return True
    return os.environ.get("NVR_PUBLIC_URL", "").lower().startswith("https://")


@dataclass(frozen=True)
class Principal:
    id: int
    username: str
    role: Role
    # None for a browser session (limited only by role); a token's granted scopes otherwise.
    scopes: frozenset[str] | None = None
    token_id: str | None = None
    # Signed in as a video wall: watching only, never the main interface.
    wall: bool = False

    def __getitem__(self, key: str) -> object:
        # Routes historically received a database row; keep subscript access.
        return getattr(self, key)

    @property
    def is_administrator(self) -> bool:
        return self.role == "administrator"


def requires_scope(scope: Scope) -> Callable[[Endpoint], Endpoint]:
    """Mark an endpoint as needing a specific token scope (media or export)."""

    def mark(endpoint: Endpoint) -> Endpoint:
        endpoint.required_scope = scope  # type: ignore[attr-defined]
        return endpoint

    return mark


def scope_for(route: object, method: str) -> Scope:
    explicit = getattr(getattr(route, "endpoint", None), "required_scope", None)
    if explicit:
        return explicit
    dependant = getattr(route, "dependant", None)
    if dependant is None or depends_on(dependant, administrator):
        return "admin"
    return "read" if method.upper() in {"GET", "HEAD"} else "admin"


def current_user(request: HTTPConnection) -> Principal:
    """Resolve the caller of an HTTP request or WebSocket handshake."""
    authorization = request.headers.get("authorization")
    if authorization is not None:
        return _token_principal(request, authorization)
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        wall = request.cookies.get(WALL_COOKIE_NAME)
        if wall:
            return wall_principal(request, wall)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    with connect() as db:
        row = db.execute(
            "SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>?",
            (token_digest(raw), datetime.now(timezone.utc).isoformat()),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    return _principal(row)


def administrator(user: Annotated[Principal, Depends(current_user)]) -> Principal:
    if user["role"] != "administrator":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator role required")
    return user


def _token_principal(request: HTTPConnection, authorization: str) -> Principal:
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise _unauthorized("Use an API token with the Bearer authorization scheme")
    try:
        row = authenticate(value.strip())
    except TokenRejected as exc:
        raise _unauthorized(str(exc)) from exc
    return _scoped(request, row)


def wall_principal(request: HTTPConnection, raw: str) -> Principal:
    """Resolve a video wall's session cookie: its token's scopes, on the routes a wall needs."""
    with connect() as db:
        session = db.execute("SELECT api_token_id FROM wall_sessions WHERE token_hash=? AND expires_at>?",
                             (token_digest(raw), datetime.now(timezone.utc).isoformat())).fetchone()
    try:
        row = usable_token(session["api_token_id"]) if session else None
    except TokenRejected:
        row = None
    if row is None or token_scopes(row) != WALL_SCOPES:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Video wall session expired")
    if getattr(request.scope.get("route"), "path", None) not in WALL_ROUTES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="A video wall can only watch cameras")
    return _scoped(request, row, wall=True)


def _scoped(request: HTTPConnection, row: sqlite3.Row, *, wall: bool = False) -> Principal:
    scopes = token_scopes(row)
    # A WebSocket handshake is a GET; its route says which scope it needs.
    required = scope_for(request.scope.get("route"), getattr(request, "method", "GET"))
    if not grants(scopes, required):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"API token lacks the '{required}' scope")
    return Principal(
        id=row["created_by"],
        username=row["owner_username"],
        role=row["owner_role"],
        scopes=scopes,
        token_id=row["id"],
        wall=wall,
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def depends_on(dependant: Dependant, target: Callable[..., object]) -> bool:
    """Whether a route's dependency tree includes ``target``."""
    return any(dependency.call is target or depends_on(dependency, target) for dependency in dependant.dependencies)


def _principal(row: sqlite3.Row) -> Principal:
    return Principal(id=row["id"], username=row["username"], role=row["role"])


CurrentUser = Annotated[Principal, Depends(current_user)]
Administrator = Annotated[Principal, Depends(administrator)]
