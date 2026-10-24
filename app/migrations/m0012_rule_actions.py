"""Rule actions: what a rule does beyond raising an alarm, and the log of each delivery.

An action's secrets (destination URLs, MQTT credentials, webhook signing keys) are
encrypted with the credential key, like camera credentials, and never returned.
Deliveries keep the action's name and type, so the log survives the action's removal.
"""
from __future__ import annotations

import sqlite3

NAME = "rule_actions"

_STATEMENTS = (
    """
    CREATE TABLE rule_actions (
        id TEXT PRIMARY KEY,
        rule_id TEXT NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        type TEXT NOT NULL CHECK (type IN ('webhook','mqtt','slack','teams','relay')),
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
        settings_json TEXT NOT NULL DEFAULT '{}',
        secret_ciphertext BLOB,
        target TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX rule_actions_rule_idx ON rule_actions(rule_id, position)",
    """
    CREATE TABLE rule_action_deliveries (
        id TEXT PRIMARY KEY,
        rule_id TEXT NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
        action_id TEXT REFERENCES rule_actions(id) ON DELETE SET NULL,
        action_name TEXT NOT NULL,
        action_type TEXT NOT NULL,
        rule_event_id TEXT NOT NULL,
        alarm_id TEXT,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sending','delivered','failed')),
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        response_status INTEGER,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        delivered_at TEXT,
        UNIQUE (action_id, rule_event_id)
    )
    """,
    "CREATE INDEX rule_action_deliveries_due_idx ON rule_action_deliveries(status, next_attempt_at)",
    "CREATE INDEX rule_action_deliveries_rule_idx ON rule_action_deliveries(rule_id, created_at)",
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
