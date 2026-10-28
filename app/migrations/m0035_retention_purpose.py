"""Retention by purpose (#139): why footage is kept, a record of what retention deleted, and the details a camera notice needs.

Each retention policy (the site default, a group's, a camera's) can carry a purpose and a legal basis. Policies that exist already upgrade with an empty
purpose; when the administrator turns on "require a purpose" they are prompted to fill it in. ``retention_deletions`` counts what the automatic clean-up
removed for each camera each day, and keeps the camera's name and the policy in force, so the record outlives a renamed or deleted camera.
"""
from __future__ import annotations

import sqlite3

NAME = "retention_purpose"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE retention_policy_overrides ADD COLUMN purpose TEXT NOT NULL DEFAULT ''")
    db.execute("ALTER TABLE retention_policy_overrides ADD COLUMN legal_basis TEXT NOT NULL DEFAULT ''")
    db.execute(
        """
        CREATE TABLE privacy_settings (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            require_purpose INTEGER NOT NULL DEFAULT 0,
            default_purpose TEXT NOT NULL DEFAULT '',
            default_legal_basis TEXT NOT NULL DEFAULT '',
            organisation TEXT NOT NULL DEFAULT '',
            contact_name TEXT NOT NULL DEFAULT '',
            contact_email TEXT NOT NULL DEFAULT '',
            contact_phone TEXT NOT NULL DEFAULT '',
            updated_at TEXT
        )
        """
    )
    db.execute("INSERT INTO privacy_settings(singleton_id) VALUES(1)")
    db.execute(
        """
        CREATE TABLE retention_deletions (
            camera_id TEXT NOT NULL,
            day TEXT NOT NULL,
            camera_name TEXT NOT NULL,
            segments INTEGER NOT NULL DEFAULT 0,
            bytes INTEGER NOT NULL DEFAULT 0,
            retention_days INTEGER NOT NULL,
            purpose TEXT NOT NULL DEFAULT '',
            legal_basis TEXT NOT NULL DEFAULT '',
            oldest_deleted_at TEXT,
            newest_deleted_at TEXT,
            PRIMARY KEY (camera_id, day)
        )
        """
    )


def verify(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(retention_policy_overrides)")}
    if not {"purpose", "legal_basis"} <= columns:
        raise RuntimeError("retention_policy_overrides lacks purpose columns")
    if db.execute("SELECT COUNT(*) FROM privacy_settings").fetchone()[0] != 1:
        raise RuntimeError("privacy_settings has no row")
