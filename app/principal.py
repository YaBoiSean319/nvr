"""Who is calling, and what they may do.

Every route declares its requirement through one of these dependencies. Today a
principal comes from a browser session cookie; scoped API tokens resolve to the
same type so routes do not care how the caller authenticated.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, status

from .security import token_digest
from .store import connect

COOKIE_NAME = "nvr_session"
Role = Literal["administrator", "operator"]


@dataclass(frozen=True)
class Principal:
    id: int
    username: str
    role: Role

    def __getitem__(self, key: str) -> object:
        # Routes historically received a database row; keep subscript access.
        return getattr(self, key)

    @property
    def is_administrator(self) -> bool:
        return self.role == "administrator"


def current_user(request: Request) -> Principal:
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


def _principal(row: sqlite3.Row) -> Principal:
    return Principal(id=row["id"], username=row["username"], role=row["role"])


CurrentUser = Annotated[Principal, Depends(current_user)]
Administrator = Annotated[Principal, Depends(administrator)]
