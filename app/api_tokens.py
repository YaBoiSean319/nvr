"""Scoped, revocable API tokens.

A token acts on behalf of the administrator who created it, limited to its scopes.
Only a keyed digest of the secret is stored; the plaintext is returned once.
"""
from __future__ import annotations

import hmac
import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from .credentials import derived_key
from .store import connect

SCOPES = ("read", "media", "export", "admin")
TOKEN_PREFIX = "nvrt"
LAST_USED_WRITE_INTERVAL = timedelta(seconds=60)


class TokenRejected(Exception):
    """The presented token is malformed, unknown, expired, revoked or orphaned."""


def grants(scopes: frozenset[str], required: str) -> bool:
    return "admin" in scopes or required in scopes


def create_token(name: str, scopes: list[str], expires_at: datetime | None, actor_id: int) -> tuple[dict[str, object], str]:
    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    normalized = sorted(set(scopes), key=SCOPES.index)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT INTO api_tokens(id,name,secret_digest,scopes_json,created_by,expires_at) VALUES(?,?,?,?,?,?)",
                (token_id, name, _digest(secret), json.dumps(normalized), actor_id, _iso(expires_at) if expires_at else None),
            )
            _audit(db, actor_id, "api_token_created", token_id, {"scopes": normalized, "expires_at": _iso(expires_at) if expires_at else None})
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        row = db.execute(_SELECT + " WHERE t.id=?", (token_id,)).fetchone()
    return token_view(row), f"{TOKEN_PREFIX}_{token_id}_{secret}"


def list_tokens() -> list[dict[str, object]]:
    with connect() as db:
        return [token_view(row) for row in db.execute(_SELECT + " ORDER BY t.created_at DESC, t.id")]


def revoke_token(token_id: str, actor_id: int) -> dict[str, object] | None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            changed = db.execute(
                "UPDATE api_tokens SET revoked_at=?,revoked_by=? WHERE id=? AND revoked_at IS NULL",
                (_iso(_now()), actor_id, token_id),
            ).rowcount
            if changed:
                _audit(db, actor_id, "api_token_revoked", token_id, {})
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        row = db.execute(_SELECT + " WHERE t.id=?", (token_id,)).fetchone()
    return token_view(row) if row else None


def authenticate(presented: str) -> sqlite3.Row:
    """Return the token joined with its creator, or raise TokenRejected."""
    parts = presented.split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX or not parts[1] or not parts[2]:
        raise TokenRejected("Invalid API token")
    token_id, secret = parts[1], parts[2]
    now = _now()
    with connect() as db:
        row = db.execute(
            "SELECT t.*,u.username AS owner_username,u.role AS owner_role FROM api_tokens t "
            "LEFT JOIN users u ON u.id=t.created_by WHERE t.id=?",
            (token_id,),
        ).fetchone()
        if row is None or not hmac.compare_digest(row["secret_digest"], _digest(secret)):
            raise TokenRejected("Invalid API token")
        _check_usable(row, now)
        last_used = _parse(row["last_used_at"]) if row["last_used_at"] else None
        if last_used is None or now - last_used >= LAST_USED_WRITE_INTERVAL:
            # Bounded writes: record use (and audit it) at most once per interval per token.
            db.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (_iso(now), token_id))
            _audit(db, row["created_by"], "api_token_used", token_id, {})
    return row


def usable_token(token_id: str) -> sqlite3.Row:
    """A token by id, for a session it signed in (a video wall), or raise TokenRejected."""
    with connect() as db:
        row = db.execute(
            "SELECT t.*,u.username AS owner_username,u.role AS owner_role FROM api_tokens t "
            "LEFT JOIN users u ON u.id=t.created_by WHERE t.id=?",
            (token_id,),
        ).fetchone()
    if row is None:
        raise TokenRejected("Invalid API token")
    _check_usable(row, _now())
    return row


def _check_usable(row: sqlite3.Row, now: datetime) -> None:
    if row["revoked_at"]:
        raise TokenRejected("API token has been revoked")
    if row["expires_at"] and _parse(row["expires_at"]) <= now:
        raise TokenRejected("API token has expired")
    if row["created_by"] is None or row["owner_role"] != "administrator":
        raise TokenRejected("API token owner is no longer an administrator")


def token_scopes(row: sqlite3.Row) -> frozenset[str]:
    return frozenset(json.loads(row["scopes_json"]))


def token_view(row: sqlite3.Row) -> dict[str, object]:
    now = _now()
    if row["revoked_at"]:
        state = "revoked"
    elif row["expires_at"] and _parse(row["expires_at"]) <= now:
        state = "expired"
    else:
        state = "active"
    return {
        "id": row["id"],
        "name": row["name"],
        "scopes": json.loads(row["scopes_json"]),
        "state": state,
        "created_by": row["created_by_username"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "last_used_at": row["last_used_at"],
    }


_SELECT = "SELECT t.*,u.username AS created_by_username FROM api_tokens t LEFT JOIN users u ON u.id=t.created_by"


def _digest(secret: str) -> str:
    return hmac.new(derived_key("api-token"), secret.encode("utf-8"), hashlib.sha256).hexdigest()


def _audit(db: sqlite3.Connection, actor_id: int | None, action: str, token_id: str, details: dict[str, object]) -> None:
    db.execute(
        "INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
        (actor_id, action, "api_token", token_id, json.dumps(details, separators=(",", ":"))),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
