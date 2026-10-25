"""Second factors for local sign-in: TOTP, passkeys (WebAuthn) and one-time recovery codes.

The TOTP secret is encrypted with the credential key. Recovery codes are stored as digests.
``mfa_challenges`` holds the short-lived state of a sign-in between its steps (the password
was right; the second factor is still owed) and of a WebAuthn ceremony. ``auth_failures``
backs sign-in rate limiting per account and per address. ``security_settings`` holds the
administrator's policy: no second factor required, required for administrators, or for all
local accounts.
"""
from __future__ import annotations

import sqlite3

NAME = "second_factors"

_STATEMENTS = (
    """
    CREATE TABLE user_totp (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        secret_ciphertext BLOB NOT NULL,
        confirmed_at TEXT,
        last_step INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE user_passkeys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        credential_id TEXT NOT NULL UNIQUE,
        public_key BLOB NOT NULL,
        algorithm INTEGER NOT NULL,
        sign_count INTEGER NOT NULL DEFAULT 0,
        transports_json TEXT NOT NULL DEFAULT '[]',
        name TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        last_used_at TEXT
    )
    """,
    "CREATE INDEX user_passkeys_user_idx ON user_passkeys(user_id)",
    """
    CREATE TABLE user_recovery_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        code_hash TEXT NOT NULL,
        used_at TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX user_recovery_codes_user_idx ON user_recovery_codes(user_id, code_hash)",
    """
    CREATE TABLE mfa_challenges (
        token_hash TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('login','passkey_login')),
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        webauthn_challenge TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    # The challenge of a passkey being registered, which belongs to a signed-in session rather than a sign-in.
    "ALTER TABLE users ADD COLUMN passkey_registration_challenge TEXT",
    "ALTER TABLE users ADD COLUMN passkey_registration_expires_at TEXT",
    # A person who prefers it may sign in with a passkey alone.
    "ALTER TABLE users ADD COLUMN passkey_sign_in INTEGER NOT NULL DEFAULT 0",
    """
    CREATE TABLE security_settings (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
        mfa_policy TEXT NOT NULL DEFAULT 'off' CHECK (mfa_policy IN ('off','administrators','all')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "INSERT INTO security_settings(singleton_id) VALUES (1)",
    """
    CREATE TABLE auth_failures (
        scope TEXT NOT NULL CHECK (scope IN ('account','address')),
        key TEXT NOT NULL,
        failures INTEGER NOT NULL,
        last_failure_at TEXT NOT NULL,
        blocked_until TEXT,
        PRIMARY KEY (scope, key)
    )
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
