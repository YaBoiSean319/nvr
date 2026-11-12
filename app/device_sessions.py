"""Device sessions for the native app (#140): a long-lived secret kept in the phone's Keychain or Keystore, exchanged for an ordinary session once the person has unlocked the phone.

The secret is shown once, when a signed-in session asks for it, and stored only as a digest. Using it replaces it, so a copy taken from an old backup is
useless; the one it replaced still works for ``GRACE`` so that a phone that never heard the answer (or could not store it) is not locked out. A person can
revoke a phone, and ending a person's sessions (a second-factor reset, a password change) ends these too. A browser session made from a device secret names its
device (#294): taking the phone away (revoked, pushed out by newer ones, or its account removed) ends those sessions in the same statement, and is audited. It is good for ``LIFETIME`` since it was last used.

The grace is a fixed deadline, set once when the replacement is first used, and the old secret can be used once inside it (#293). Replaying an old
secret therefore cannot keep it alive, and cannot knock a phone's current secret out more than once per rotation. A thief who replays the old secret
inside those two minutes wins that one race, and the phone then has to sign in again; the audit log shows it as ``device_session_retried``.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from .security import new_session_token, token_digest
from .store import connect
from app.clock import now as _now, stamp

LIFETIME = timedelta(days=30)
GRACE = timedelta(minutes=2)
MAX_PER_USER = 10
PREFIX = "nvrd_"


def _plausible(token: str) -> bool:
    return token.startswith(PREFIX) and len(token) <= 200 and token.isascii()


def _audit(db, actor_id: int, action: str, session_id: str, **details: Any) -> None:
    db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
               (actor_id, action, "device_session", session_id, json.dumps(details)))


def _remove(db, user_id: int, session_id: str, action: str) -> bool:
    """Take a device away: its secret, and (through the foreign key, in the same statement) every browser session it produced (#294). Audited with how many."""
    row = db.execute("SELECT device_label FROM device_sessions WHERE id=? AND user_id=?", (session_id, user_id)).fetchone()
    if row is None:
        return False
    ended = db.execute("SELECT COUNT(*) FROM sessions WHERE device_session_id=?", (session_id,)).fetchone()[0]
    db.execute("DELETE FROM device_sessions WHERE id=?", (session_id,))
    _audit(db, user_id, action, session_id, device=row["device_label"], sessions_ended=ended)
    return True


def create(user_id: int, label: str) -> dict[str, Any]:
    """A new device session for a signed-in person; the least recently used go once there are too many."""
    raw = PREFIX + new_session_token()
    session_id, expires = str(uuid.uuid4()), stamp(_now() + LIFETIME)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("DELETE FROM device_sessions WHERE expires_at<=?", (stamp(_now()),))
            old = [row[0] for row in db.execute("SELECT id FROM device_sessions WHERE user_id=? ORDER BY COALESCE(last_used_at,created_at) DESC", (user_id,))][MAX_PER_USER - 1:]
            for stale in old:
                _remove(db, user_id, stale, "device_session_evicted")
            db.execute("INSERT INTO device_sessions(id,user_id,token_hash,device_label,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                       (session_id, user_id, token_digest(raw), label, stamp(_now()), expires))
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
    expires = stamp(now + LIFETIME)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT s.*,u.username,u.role FROM device_sessions s JOIN users u ON u.id=s.user_id "
                             "WHERE s.token_hash=? OR (s.previous_hash=? AND s.previous_until>?)", (digest, digest, stamp(now))).fetchone()
            if row is None or row["expires_at"] <= stamp(now):
                db.execute("ROLLBACK")
                return None
            retry = digest != row["token_hash"]
            if retry:
                # The previous secret, presented again: a phone that never heard the answer asking once more. It works this once, nothing is extended
                # (its deadline was fixed when it was replaced), and it is spent: the replacement it is handed is the only secret that lasts.
                previous_hash, previous_until = None, None
            else:
                # The secret in use becomes the "previous" one for GRACE from now; this is the only place that deadline is set.
                previous_hash, previous_until = digest, stamp(now + GRACE)
            db.execute("UPDATE device_sessions SET token_hash=?,previous_hash=?,previous_until=?,last_used_at=?,expires_at=? WHERE id=?",
                       (token_digest(raw), previous_hash, previous_until, stamp(now), expires, row["id"]))
            _audit(db, int(row["user_id"]), "device_session_retried" if retry else "device_session_resumed", row["id"], device=row["device_label"])
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {"user_id": int(row["user_id"]), "username": row["username"], "role": row["role"], "id": row["id"], "token": raw, "expires_at": expires}


def listing(user_id: int) -> list[dict[str, Any]]:
    with connect() as db:
        return [{"id": row["id"], "device_label": row["device_label"], "created_at": row["created_at"], "last_used_at": row["last_used_at"], "expires_at": row["expires_at"]}
                for row in db.execute("SELECT * FROM device_sessions WHERE user_id=? AND expires_at>? ORDER BY COALESCE(last_used_at,created_at) DESC", (user_id, stamp(_now())))]


def revoke(user_id: int, session_id: str) -> bool:
    """Remove a phone by its id. Its secret stops working and so does every browser session it was given, at once."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            removed = _remove(db, user_id, session_id, "device_session_revoked")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return removed


def revoke_token(user_id: int, token: str) -> bool:
    """End the session this secret belongs to (the phone signing itself out), if it is this person's."""
    if not _plausible(token):
        return False
    digest = token_digest(token)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            found = db.execute("SELECT id FROM device_sessions WHERE user_id=? AND (token_hash=? OR previous_hash=?)", (user_id, digest, digest)).fetchone()
            removed = _remove(db, user_id, found["id"], "device_session_revoked") if found else False
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return removed
