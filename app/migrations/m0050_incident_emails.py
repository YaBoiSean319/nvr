"""Emails about the NVR itself and emails held back (#231): who is told about problems (camera offline, disk, the AI worker...), the queue those emails go through, and
how many alert emails the rate limit has held back since the last summary.
"""
from __future__ import annotations

import sqlite3

NAME = "incident_emails"

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS incident_emails (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
        enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
        recipients_json TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS email_outbox (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('incident','held_summary')),
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        recipients_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sending','sent','failed')),
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        sent_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS email_outbox_due_idx ON email_outbox(status, next_attempt_at)",
    """
    CREATE TABLE IF NOT EXISTS email_held (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
        held INTEGER NOT NULL DEFAULT 0,
        first_held_at TEXT,
        last_held_at TEXT
    )
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)


def verify(db: sqlite3.Connection) -> None:
    for table in ("incident_emails", "email_outbox", "email_held"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            raise RuntimeError(f"{table} was not created")
