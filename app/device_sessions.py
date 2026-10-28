"""Device sessions for the native app (#140): a long-lived secret kept in the phone's Keychain or Keystore, exchanged for an ordinary session once the person has unlocked the phone.

The secret is shown once, when a signed-in session asks for it, and stored only as a digest. Using it replaces it, so a copy taken from an old backup is
useless, and a person (or an administrator, by ending the account's sessions) can revoke a phone. It is good for ``LIFETIME`` since it was last used.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .security import new_session_token, token_digest
from .store import connect

LIFETIME = timedelta(days=30)
MAX_PER_USER = 10
PREFIX = "nvrd_"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _issue(db, user_id: int, label: str, session_id: str | None = None) -> tuple[str, str, str]:
    raw = PREFIX + new_session_token()
    session_id = session_id or str(uuid.uuid4())
    expires = (_now() + LIFETIME).isoformat()
    db.execute("INSERT INTO device_sessions(id,user_id,token_hash,device_label,created_at,expires_at) VALUES(?,?,?,?,?,?)",
               (session_id, user_id, token_digest(raw), label, _now().isoformat(), expires))
    return raw, session_id, expires


def create(user_id: int, label: str) -> dict[str, Any]:
    """A new device session for a signed-in person; the oldest go once there are too many."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("DELETE FROM device_sessions WHERE expires_at<=?", (_now().isoformat(),))
            old = [row[0] for row in db.execute("SELECT id FROM device_sessions WHERE user_id=? ORDER BY COALESCE(last_used_at,created_at) DESC", (user_id,))][MAX_PER_USER - 1:]
            for session_id in old:
                db.execute("DELETE FROM device_sessions WHERE id=?", (session_id,))
            raw, session_id, expires = _issue(db, user_id, label)
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                       (user_id, "device_session_created", "device_session", session_id, '{"device":%s}' % _json(label)))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {"id": session_id, "token": raw, "expires_at": expires, "device_label": label}


def resume(token: str) -> dict[str, Any] | None:
    """Exchange a device secret for the person it belongs to and a replacement secret; ``None`` for anything that is not a live one."""
    if not token.startswith(PREFIX) or len(token) > 200:
        return None
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT s.*,u.username,u.role FROM device_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (token_digest(token),)).fetchone()
            if row is None or row["expires_at"] <= _now().isoformat():
                db.execute("ROLLBACK")
                return None
            db.execute("DELETE FROM device_sessions WHERE id=?", (row["id"],))
            raw, _, expires = _issue(db, int(row["user_id"]), row["device_label"], row["id"])
            db.execute("UPDATE device_sessions SET last_used_at=? WHERE id=?", (_now().isoformat(), row["id"]))
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
    with connect() as db:
        return db.execute("DELETE FROM device_sessions WHERE token_hash=? AND user_id=?", (token_digest(token), user_id)).rowcount > 0


def _json(text: str) -> str:
    import json
    return json.dumps(text)
