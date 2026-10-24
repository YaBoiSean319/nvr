"""Web Push for alarms: the installation's VAPID keys, device subscriptions, each
person's notification choices and the delivery queue.

The VAPID private key is encrypted with the credential key and lives in the
database, so configuration backups carry it and a restore keeps every existing
subscription working.
"""
from __future__ import annotations

import sqlite3

NAME = "push"

_STATEMENTS = (
    """
    CREATE TABLE push_keys (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
        private_key_ciphertext BLOB NOT NULL,
        public_key TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE push_subscriptions (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        endpoint TEXT NOT NULL UNIQUE,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        device_label TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        last_success_at TEXT,
        last_failure_at TEXT
    )
    """,
    """
    CREATE INDEX push_subscriptions_user_idx ON push_subscriptions(user_id)
    """,
    """
    CREATE TABLE push_preferences (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        rule_ids_json TEXT,
        group_ids_json TEXT,
        quiet_start TEXT,
        quiet_end TEXT,
        quiet_timezone TEXT,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE push_deliveries (
        id TEXT PRIMARY KEY,
        subscription_id TEXT NOT NULL REFERENCES push_subscriptions(id) ON DELETE CASCADE,
        alarm_id TEXT REFERENCES alarms(id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sending','sent','failed')),
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        last_error TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        UNIQUE (subscription_id, alarm_id)
    )
    """,
    """
    CREATE INDEX push_deliveries_due_idx ON push_deliveries(status,next_attempt_at)
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
