"""Access grants: which cameras an operator may see, and until when.

A grant gives a user (or everyone holding an Entra role) access to every camera, or to one
camera group, optionally until an expiry. Administrators need none. Every operator that
exists at upgrade gets an "all cameras" grant, so nothing changes until an administrator
narrows it, and a trigger gives each operator created later the same default (unless
they hold an Entra role that has grants of its own). An operator with no grants sees no cameras.
"""
from __future__ import annotations

import sqlite3

NAME = "access_grants"

_STATEMENTS = (
    # The roles an Entra user held at their last sign-in, which role grants are matched against.
    "ALTER TABLE users ADD COLUMN provider_roles_json TEXT NOT NULL DEFAULT '[]'",
    """
    CREATE TABLE access_grants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_type TEXT NOT NULL CHECK (subject_type IN ('user','role')),
        subject TEXT NOT NULL,
        scope_type TEXT NOT NULL CHECK (scope_type IN ('all','group')),
        group_id TEXT REFERENCES camera_groups(id) ON DELETE CASCADE,
        expires_at TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        CHECK ((scope_type='all' AND group_id IS NULL) OR (scope_type='group' AND group_id IS NOT NULL))
    )
    """,
    "CREATE INDEX access_grants_subject_idx ON access_grants(subject_type, subject)",
    "CREATE INDEX access_grants_group_idx ON access_grants(group_id)",
    "INSERT INTO access_grants(subject_type,subject,scope_type) SELECT 'user', CAST(id AS TEXT), 'all' FROM users WHERE role='operator'",
    """
    CREATE TRIGGER access_grants_default_operator AFTER INSERT ON users
    WHEN NEW.role='operator' AND NOT EXISTS (
        SELECT 1 FROM access_grants g WHERE g.subject_type='role' AND g.subject IN (SELECT value FROM json_each(NEW.provider_roles_json)))
    BEGIN
        INSERT INTO access_grants(subject_type,subject,scope_type) VALUES ('user', CAST(NEW.id AS TEXT), 'all');
    END
    """,
    # A grant for a user who is deleted goes with them.
    """
    CREATE TRIGGER access_grants_user_deleted AFTER DELETE ON users
    BEGIN
        DELETE FROM access_grants WHERE subject_type='user' AND subject=CAST(OLD.id AS TEXT);
    END
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
