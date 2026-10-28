"""The native mobile app (#140): push to its devices through FCM and APNs, and device sessions that unlock with the phone's biometrics.

Native devices use the same subscription model as web push (``push_subscriptions`` with a ``kind``), so each person's rule, group and quiet-hours choices,
the access checks and the delivery queue with its retries apply to them unchanged. ``native_push_settings`` holds the administrator's FCM service account
and APNs key (encrypted, write-only). A *device session* is a long-lived secret the app keeps in the phone's Keychain or Keystore and exchanges, once the
person has unlocked the phone, for an ordinary session; it is stored hashed, replaced each time it is used, and can be revoked.
"""
from __future__ import annotations

import sqlite3

NAME = "native_app"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE push_subscriptions ADD COLUMN kind TEXT NOT NULL DEFAULT 'web' CHECK (kind IN ('web','fcm','apns'))")
    db.execute(
        """
        CREATE TABLE native_push_settings (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            fcm_project_id TEXT,
            fcm_credentials_ciphertext BLOB,
            apns_key_ciphertext BLOB,
            apns_key_id TEXT,
            apns_team_id TEXT,
            apns_bundle_id TEXT,
            apns_production INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT
        )
        """
    )
    db.execute("INSERT INTO native_push_settings(singleton_id) VALUES(1)")
    db.execute(
        """
        CREATE TABLE device_sessions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            device_label TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            expires_at TEXT NOT NULL
        )
        """
    )
    db.execute("CREATE INDEX device_sessions_user_idx ON device_sessions(user_id)")


def verify(db: sqlite3.Connection) -> None:
    if "kind" not in {row[1] for row in db.execute("PRAGMA table_info(push_subscriptions)")}:
        raise RuntimeError("push_subscriptions has no kind column")
    if db.execute("SELECT COUNT(*) FROM native_push_settings").fetchone()[0] != 1:
        raise RuntimeError("native_push_settings has no row")
