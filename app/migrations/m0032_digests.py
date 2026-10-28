"""Event digests (#136): written summaries of a time range, scheduled per shift and delivered through actions.

A schedule says what to cover (the site, groups or cameras), whose view of it (the recipient: a digest never lists a camera that person may not
see), when each shift ends, and where to send it (actions: signed webhook, Slack, Teams or MQTT, with encrypted destinations like rule actions).
Each digest made is kept with what it found. ``digest_settings`` holds the optional summarising model endpoint, off until an administrator
configures it.
"""
from __future__ import annotations

import sqlite3

NAME = "digests"

_STATEMENTS = (
    """
    CREATE TABLE digest_settings (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
        summary_enabled INTEGER NOT NULL DEFAULT 0 CHECK (summary_enabled IN (0,1)),
        summary_url_ciphertext BLOB,
        summary_model TEXT NOT NULL DEFAULT '',
        summary_allow_private INTEGER NOT NULL DEFAULT 0 CHECK (summary_allow_private IN (0,1)),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "INSERT INTO digest_settings(singleton_id) VALUES(1)",
    """
    CREATE TABLE digest_schedules (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
        scope_json TEXT NOT NULL,
        recipient_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        shift_json TEXT NOT NULL,
        after_hours_json TEXT NOT NULL,
        summarise INTEGER NOT NULL DEFAULT 0 CHECK (summarise IN (0,1)),
        last_period_end TEXT NOT NULL,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE digest_actions (
        id TEXT PRIMARY KEY,
        schedule_id TEXT NOT NULL REFERENCES digest_schedules(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        type TEXT NOT NULL CHECK (type IN ('webhook','mqtt','slack','teams')),
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
        settings_json TEXT NOT NULL DEFAULT '{}',
        secret_ciphertext BLOB,
        target TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX digest_actions_schedule_idx ON digest_actions(schedule_id, position)",
    """
    CREATE TABLE digests (
        id TEXT PRIMARY KEY,
        schedule_id TEXT REFERENCES digest_schedules(id) ON DELETE SET NULL,
        name TEXT NOT NULL,
        recipient_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        body_json TEXT NOT NULL,
        text TEXT NOT NULL,
        summary TEXT,
        summary_error TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (schedule_id, period_end)
    )
    """,
    "CREATE INDEX digests_recipient_idx ON digests(recipient_user_id, period_end DESC)",
    """
    CREATE TABLE digest_deliveries (
        id TEXT PRIMARY KEY,
        digest_id TEXT NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
        action_id TEXT REFERENCES digest_actions(id) ON DELETE SET NULL,
        action_name TEXT NOT NULL,
        action_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sending','delivered','failed')),
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        response_status INTEGER,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        delivered_at TEXT,
        UNIQUE (action_id, digest_id)
    )
    """,
    "CREATE INDEX digest_deliveries_due_idx ON digest_deliveries(status, next_attempt_at)",
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
