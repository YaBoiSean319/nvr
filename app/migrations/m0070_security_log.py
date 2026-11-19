"""The security log can be read, filtered and pruned (#367).

``audit_log`` holds nearly every sensitive action, and nothing ever read it in the product or removed from it. It gets a retention period of
its own beside the viewing audit's, and two indexes so that the page's filters (by person and by action, newest first) and the retention
pass do not read the whole table.
"""
from __future__ import annotations

import sqlite3

NAME = "security_log"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE security_settings ADD COLUMN security_log_retention_days INTEGER NOT NULL DEFAULT 730")
    db.execute("CREATE INDEX IF NOT EXISTS audit_log_actor_idx ON audit_log(actor_id, created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS audit_log_action_idx ON audit_log(action, created_at)")
