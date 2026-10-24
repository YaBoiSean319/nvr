"""Scoped, revocable API tokens for automation."""
from __future__ import annotations

import sqlite3

NAME = "api_tokens"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE api_tokens (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            secret_digest TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            expires_at TEXT,
            revoked_at TEXT,
            revoked_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            last_used_at TEXT
        )
        """
    )
    db.execute("CREATE INDEX api_tokens_created_idx ON api_tokens(created_at DESC)")
