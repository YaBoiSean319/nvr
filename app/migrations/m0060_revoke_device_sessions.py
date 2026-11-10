"""Phones enrolled before enrolment was limited to a signed-in person (#292) are signed out once.

Until this release an administrator's API token could create a phone's device secret, exchange it for a browser session, and keep that session after the token was
revoked. Nothing recorded whether a device secret was made that way, so none of the existing ones can be trusted. Each is ended, and written to the audit log with the
person it belonged to, so the person can see why their phone asked to sign in again. Signing in once on the phone makes a new secret. Browser sessions already open are
not touched (they carry no link to a device and end on their own, within the session lifetime).
"""
from __future__ import annotations

import json
import sqlite3

NAME = "revoke_device_sessions"
REASON = "Enrolled before phones could only be enrolled by a signed-in person; the way it was made was not recorded"


def upgrade(db: sqlite3.Connection) -> None:
    for row in db.execute("SELECT id,user_id,device_label FROM device_sessions").fetchall():
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (row[1], "device_session_revoked_by_upgrade", "device_session", row[0], json.dumps({"device": row[2], "reason": REASON})))
    db.execute("DELETE FROM device_sessions")
