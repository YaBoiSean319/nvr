"""Multi-site console (#141): other NVRs (sites) federated into this one through scoped API tokens, and the grants that say who sees which site.

A site keeps its own recording, storage and configuration; the console holds only how to reach it. The token is encrypted like a camera credential and is never
returned. ``site_grants`` gives an operator (or an Entra role) every camera of one site; the existing grants (every camera, or one camera group) are about this
NVR's own cameras and groups, and a grant for every camera covers every site as well.
"""
from __future__ import annotations

import sqlite3

NAME = "console"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE console_settings (singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1), enabled INTEGER NOT NULL DEFAULT 0, updated_at TEXT)")
    db.execute("INSERT INTO console_settings(singleton_id) VALUES(1)")
    db.execute(
        """
        CREATE TABLE console_sites (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            base_url TEXT NOT NULL,
            token_ciphertext BLOB NOT NULL,
            allow_private INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            last_contact_at TEXT,
            last_checked_at TEXT,
            last_error TEXT,
            camera_count INTEGER,
            open_alarms INTEGER,
            site_version TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE site_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_type TEXT NOT NULL CHECK (subject_type IN ('user','role')),
            subject TEXT NOT NULL,
            site_id TEXT NOT NULL REFERENCES console_sites(id) ON DELETE CASCADE,
            expires_at TEXT,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE (subject_type, subject, site_id)
        )
        """
    )
    db.execute("CREATE INDEX site_grants_subject_idx ON site_grants(subject_type, subject)")
    # A grant for a user who is deleted goes with them, as access_grants' do.
    db.execute("CREATE TRIGGER site_grants_user_deleted AFTER DELETE ON users BEGIN DELETE FROM site_grants WHERE subject_type='user' AND subject=CAST(OLD.id AS TEXT); END")


def verify(db: sqlite3.Connection) -> None:
    if db.execute("SELECT COUNT(*) FROM console_settings").fetchone()[0] != 1:
        raise RuntimeError("console_settings has no row")
