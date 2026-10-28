"""Device sessions for the native app (#140): a long-lived secret kept in the phone's Keychain or Keystore, exchanged for an ordinary session once the person has unlocked the phone.

The secret is shown once, when a signed-in session asks for it, and stored only as a digest. Using it replaces it, so a copy taken from an old backup is
useless; the one it replaced still works for ``GRACE`` so that a phone that never heard the answer (or could not store it) is not locked out. A person can
revoke a phone, and ending a person's sessions (a second-factor reset, a password change) ends these too. It is good for ``LIFETIME`` since it was last used.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .security import new_session_token, token_digest
from .store import connect

LIFETIME = timedelta(days=30)
GRACE = timedelta(minutes=2)
MAX_PER_USER = 10
PREFIX = "nvrd_"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plausible(token: str) -> bool:
    return token.startswith(PREFIX) and len(token) <= 200 and token.isascii()


def _audit(db, actor_id: int, action: str, session_id: str, **details: Any) -> None:
    db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
               (actor_id, action, "device_session", session_id, json.dumps(details)))


def create(user_id: int, label: str) -> dict[str, Any]:
    """A new device session for a signed-in person; the least recently used go once there are too many."""
    raw = PREFIX + new_session_token()
    session_id, expires = str(uuid.uuid4()), (_now() + LIFETIME).isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("DELETE FROM device_sessions WHERE expires_at<=?", (_now().isoformat(),))
            old = [row[0] for row in db.execute("SELECT id FROM device_sessions WHERE user_id=? ORDER BY COALESCE(last_used_at,created_at) DESC", (user_id,))][MAX_PER_USER - 1:]
            for stale in old:
                db.execute("DELETE FROM device_sessions WHERE id=?", (stale,))
            db.execute("INSERT INTO device_sessions(id,user_id,token_hash,device_label,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                       (session_id, user_id, token_digest(raw), label, _now().isoformat(), expires))
            _audit(db, user_id, "device_session_created", session_id, device=label)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {"id": session_id, "token": raw, "expires_at": expires, "device_label": label}


def resume(token: str) -> dict[str, Any] | None:
    """Exchange a device secret for the person it belongs to and a replacement secret; ``None`` for anything that is not a live one."""
    if not _plausible(token):
        return None
    digest, now = token_digest(token), _now()
    raw = PREFIX + new_session_token()
    expires = (now + LIFETIME).isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT s.*,u.username,u.role FROM device_sessions s JOIN users u ON u.id=s.user_id "
                             "WHERE s.token_hash=? OR (s.previous_hash=? AND s.previous_until>?)", (digest, digest, now.isoformat())).fetchone()
            if row is None or row["expires_at"] <= now.isoformat():
                db.execute("ROLLBACK")
                return None
            # The secret in use becomes the "previous" one for a moment; the replacement is the only one that lasts.
            db.execute("UPDATE device_sessions SET token_hash=?,previous_hash=?,previous_until=?,last_used_at=?,expires_at=? WHERE id=?",
                       (token_digest(raw), digest if digest == row["token_hash"] else row["previous_hash"], (now + GRACE).isoformat(), now.isoformat(), expires, row["id"]))
            _audit(db, int(row["user_id"]), "device_session_resumed", row["id"], device=row["device_label"])
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {"user_id": int(row["user_id"]), "username": row["username"], "role": row["role"], "id": row["id"], "token": raw, "expires_at": expires}


def listing(user_id: int) -> list[dict[str, Any]]:
    with connect() as db:
        return [{"id": row["id"], "device_label": row["device_label"], "created_at": row["created_at"], "last_used_at": row["last_used_at"], "expires_at": row["expires_at"]}
                for row in db.execute("SELECT * FROM device_sessions WHERE user_id=? AND expires_at>? ORDER BY COALESCE(last_used_at,created_at) DESC", (user_id, _now().isoformat()))]


def revoke(user_id: int, session_id: str) -> bool:
    with connect() as db:
        return db.execute("DELETE FROM device_sessions WHERE id=? AND user_id=?", (session_id, user_id)).rowcount > 0


def revoke_token(user_id: int, token: str) -> bool:
    """End the session this secret belongs to (the phone signing itself out), if it is this person's."""
    if not _plausible(token):
        return False
    digest = token_digest(token)
    with connect() as db:
        return db.execute("DELETE FROM device_sessions WHERE user_id=? AND (token_hash=? OR previous_hash=?)", (user_id, digest, digest)).rowcount > 0
