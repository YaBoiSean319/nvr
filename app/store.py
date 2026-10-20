from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def database_path() -> Path:
    return Path(os.environ.get("NVR_DB_PATH", "./data/nvr.sqlite3"))


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    return db


def initialize() -> None:
    with connect() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('administrator','operator')),
                provider_issuer TEXT,
                provider_subject TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS camera_groups (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT NOT NULL UNIQUE,
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS cameras (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                onvif_uuid TEXT,
                group_id TEXT REFERENCES camera_groups(id) ON DELETE SET NULL,
                stream_url_ciphertext BLOB NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
                recording_mode TEXT NOT NULL DEFAULT 'motion' CHECK (recording_mode IN ('motion','continuous','disabled')),
                recording_status TEXT NOT NULL DEFAULT 'stopped',
                video_codec TEXT,
                video_width INTEGER,
                video_height INTEGER,
                last_probe_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS recording_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL UNIQUE,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS recording_camera_time_idx ON recording_segments(camera_id, starts_at, ends_at);
            CREATE TABLE IF NOT EXISTS discovery_candidates (
                id TEXT PRIMARY KEY,
                device_uuid TEXT NOT NULL UNIQUE,
                service_urls TEXT NOT NULL,
                scopes TEXT NOT NULL,
                capabilities TEXT NOT NULL,
                linked_camera_id TEXT REFERENCES cameras(id) ON DELETE SET NULL,
                first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                reviewed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS diagnostic_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS diagnostic_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                event_type TEXT NOT NULL,
                fields_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS diagnostic_events_created_idx ON diagnostic_events(created_at);
            """
        )
        # Migrate installations created by the first implementation, which only
        # allowed administrator accounts and did not assign stable session IDs.
        camera_columns = {row[1] for row in db.execute("PRAGMA table_info(cameras)")}
        if "onvif_uuid" not in camera_columns:
            db.execute("ALTER TABLE cameras ADD COLUMN onvif_uuid TEXT")
        if "group_id" not in camera_columns:
            db.execute("ALTER TABLE cameras ADD COLUMN group_id TEXT REFERENCES camera_groups(id) ON DELETE SET NULL")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS cameras_onvif_uuid_idx ON cameras(onvif_uuid) WHERE onvif_uuid IS NOT NULL")
        user_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()[0]
        user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "operator" not in user_sql:
            db.execute("PRAGMA foreign_keys = OFF")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE users_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('administrator','operator')),
                provider_issuer TEXT,
                provider_subject TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            db.execute("INSERT INTO users_new(id,username,password_hash,role,created_at) SELECT id,username,password_hash,role,created_at FROM users")
            db.execute("CREATE TEMP TABLE sessions_backup AS SELECT lower(hex(randomblob(16))) AS session_id,token_hash,user_id,expires_at,created_at FROM sessions")
            db.execute("DROP TABLE sessions")
            db.execute("DROP TABLE users")
            db.execute("ALTER TABLE users_new RENAME TO users")
            db.execute("""CREATE TABLE sessions (
                session_id TEXT NOT NULL UNIQUE,
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            db.execute("INSERT INTO sessions(session_id,token_hash,user_id,expires_at,created_at) SELECT session_id,token_hash,user_id,expires_at,created_at FROM sessions_backup")
            db.execute("DROP TABLE sessions_backup")
            db.execute("CREATE INDEX sessions_expiry_idx ON sessions(expires_at)")
            db.commit()
            db.execute("PRAGMA foreign_keys = ON")
        else:
            if "provider_issuer" not in user_columns:
                db.execute("ALTER TABLE users ADD COLUMN provider_issuer TEXT")
            if "provider_subject" not in user_columns:
                db.execute("ALTER TABLE users ADD COLUMN provider_subject TEXT")
            session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            if "session_id" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN session_id TEXT")
                db.execute("UPDATE sessions SET session_id=lower(hex(randomblob(16))) WHERE session_id IS NULL")
                db.execute("CREATE UNIQUE INDEX sessions_id_idx ON sessions(session_id)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_provider_identity_idx ON users(provider_issuer, provider_subject) WHERE provider_issuer IS NOT NULL AND provider_subject IS NOT NULL")
