"""Case files (#129): everything about one incident in one place.

A case has an owner, members and a status. Its items are clips (a camera and a time range), marks, notes, alarms, saved searches and
exports. Footage of an open case's clips is held through ``recording_holds`` rows that carry the case's id, so the existing hold rules
keep it from being deleted; closing a case (or removing a clip) releases only the holds that case placed. ``case_audit`` records who
opened, changed, exported or closed it. A package is one evidence file for the whole case, built from an export of each clip.
"""
from __future__ import annotations

import sqlite3

NAME = "cases"


_STATEMENTS = (
    """
    CREATE TABLE cases (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
        owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        closed_at TEXT,
        closed_by INTEGER REFERENCES users(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE case_members (
        case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        added_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        added_at TEXT NOT NULL,
        PRIMARY KEY (case_id, user_id)
    )
    """,
    """
    CREATE INDEX case_members_user_idx ON case_members(user_id)
    """,
    """
    CREATE TABLE case_items (
        id TEXT PRIMARY KEY,
        case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('clip','mark','note','alarm','search','export')),
        camera_id TEXT,
        starts_at TEXT,
        ends_at TEXT,
        ref TEXT,
        text TEXT NOT NULL DEFAULT '',
        data_json TEXT NOT NULL DEFAULT '{}',
        added_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        added_at TEXT NOT NULL,
        removed_at TEXT,
        removed_by INTEGER REFERENCES users(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE INDEX case_items_case_idx ON case_items(case_id, added_at)
    """,
    """
    CREATE TABLE case_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
        actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        action TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX case_audit_case_idx ON case_audit(case_id, id)
    """,
    """
    CREATE TABLE case_packages (
        id TEXT PRIMARY KEY,
        case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
        requested_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'building' CHECK (status IN ('building','ready','failed')),
        error TEXT,
        omitted INTEGER NOT NULL DEFAULT 0,
        relative_path TEXT,
        size_bytes INTEGER,
        sha256 TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE case_package_clips (
        package_id TEXT NOT NULL REFERENCES case_packages(id) ON DELETE CASCADE,
        item_id TEXT NOT NULL,
        export_id TEXT,
        PRIMARY KEY (package_id, item_id)
    )
    """,
    """
    ALTER TABLE recording_holds ADD COLUMN case_id TEXT
    """,
    """
    CREATE INDEX recording_holds_case_idx ON recording_holds(case_id, released_at)
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
