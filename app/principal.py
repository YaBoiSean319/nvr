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
cookies.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Callable, Literal, TypeVar

from fastapi import Depends, HTTPException, Request, status
from fastapi.dependencies.models import Dependant

from .api_tokens import TokenRejected, authenticate, grants, token_scopes
from .security import token_digest
from .store import connect

COOKIE_NAME = "nvr_session"
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


def current_user(request: Request) -> Principal:
    authorization = request.headers.get("authorization")
    if authorization is not None:
        return _token_principal(request, authorization)
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
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


def _token_principal(request: Request, authorization: str) -> Principal:
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise _unauthorized("Use an API token with the Bearer authorization scheme")
    try:
        row = authenticate(value.strip())
    except TokenRejected as exc:
        raise _unauthorized(str(exc)) from exc
    scopes = token_scopes(row)
    required = scope_for(request.scope.get("route"), request.method)
    if not grants(scopes, required):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"API token lacks the '{required}' scope")
    return Principal(
        id=row["created_by"],
        username=row["owner_username"],
        role=row["owner_role"],
        scopes=scopes,
        token_id=row["id"],
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
