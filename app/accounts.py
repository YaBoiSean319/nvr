"""Taking a person's access away (#364).

A person reaches the NVR through a browser session, a device sign-in (a phone's stored secret, which hands out sessions), API tokens they made,
and alarm notifications pushed to their devices. ``end_access`` ends all four in the caller's transaction. It is used when an administrator
disables an account and when Entra says the person no longer has a role here."""
from __future__ import annotations

import sqlite3

from .audit import record
from .clock import stamp


def end_access(db: sqlite3.Connection, user_id: int, actor_id: int | None) -> dict[str, int]:
    """End every session, device sign-in and push subscription of a person and revoke their API tokens. Returns how many of each."""
    ended = {
        "sessions": db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,)).rowcount,
        "device_sign_ins": db.execute("DELETE FROM device_sessions WHERE user_id=?", (user_id,)).rowcount,
        "push_subscriptions": db.execute("DELETE FROM push_subscriptions WHERE user_id=?", (user_id,)).rowcount,
        "api_tokens": db.execute("UPDATE api_tokens SET revoked_at=?,revoked_by=? WHERE created_by=? AND revoked_at IS NULL", (stamp(), actor_id, user_id)).rowcount,
    }
    db.execute("DELETE FROM mfa_challenges WHERE user_id=?", (user_id,))  # A sign-in half done does not finish
    return ended


def set_disabled(db: sqlite3.Connection, user_id: int, disabled: bool, actor_id: int) -> bool:
    """Disable or enable an account; False when it was already so. Disabling ends its access; enabling restores nothing that was ended."""
    row = db.execute("SELECT username,disabled_at FROM users WHERE id=?", (user_id,)).fetchone()
    if (row["disabled_at"] is not None) == disabled:
        return False
    if disabled:
        db.execute("UPDATE users SET disabled_at=? WHERE id=?", (stamp(), user_id))
        record(db, actor_id, "user_disabled", "user", user_id, username=row["username"], **end_access(db, user_id, actor_id))
    else:
        db.execute("UPDATE users SET disabled_at=NULL WHERE id=?", (user_id,))
        record(db, actor_id, "user_enabled", "user", user_id, username=row["username"])
    return True
