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
                storage_volume_id TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS storage_volumes (
                id TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL,
                configured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS recording_policy_overrides (
                scope_type TEXT NOT NULL CHECK (scope_type IN ('group','camera')),
                scope_id TEXT NOT NULL,
                timezone_name TEXT NOT NULL,
                schedule_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY(scope_type, scope_id)
            );
            CREATE TABLE IF NOT EXISTS retention_policy_overrides (
                scope_type TEXT NOT NULL CHECK (scope_type IN ('group','camera')),
                scope_id TEXT NOT NULL,
                retention_days INTEGER NOT NULL CHECK (retention_days BETWEEN 1 AND 3650),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY(scope_type, scope_id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT NOT NULL UNIQUE,
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS viewer_layouts (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL COLLATE NOCASE,
                camera_ids_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY(user_id,name)
            );
            CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS cameras (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                onvif_uuid TEXT,
                group_id TEXT REFERENCES camera_groups(id) ON DELETE SET NULL,
                stream_url_ciphertext BLOB NOT NULL,
                live_substream_url_ciphertext BLOB,
                audio_supported INTEGER CHECK (audio_supported IN (0,1)),
                audio_enabled INTEGER NOT NULL DEFAULT 0 CHECK (audio_enabled IN (0,1)),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
                recording_mode TEXT NOT NULL DEFAULT 'motion' CHECK (recording_mode IN ('motion','continuous','disabled')),
                recording_enabled INTEGER NOT NULL DEFAULT 0 CHECK (recording_enabled IN (0,1)),
                recording_status TEXT NOT NULL DEFAULT 'stopped',
                last_video_at TEXT,
                last_footage_write_at TEXT,
                recording_fault_since TEXT,
                motion_service_url TEXT,
                motion_credentials_ciphertext BLOB,
                motion_pre_roll_seconds INTEGER NOT NULL DEFAULT 10,
                motion_post_roll_seconds INTEGER NOT NULL DEFAULT 30,
                motion_source_status TEXT NOT NULL DEFAULT 'untested',
                motion_fallback_enabled INTEGER NOT NULL DEFAULT 0 CHECK (motion_fallback_enabled IN (0,1)),
                motion_zones_json TEXT NOT NULL DEFAULT '[]',
                motion_sensitivity INTEGER NOT NULL DEFAULT 50,
                server_motion_status TEXT NOT NULL DEFAULT 'disabled',
                video_codec TEXT,
                video_width INTEGER,
                video_height INTEGER,
                last_probe_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS camera_group_memberships (
                group_id TEXT NOT NULL REFERENCES camera_groups(id) ON DELETE CASCADE,
                camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY(group_id,camera_id)
            );
            CREATE INDEX IF NOT EXISTS camera_group_memberships_camera_idx
                ON camera_group_memberships(camera_id,group_id);
            CREATE TABLE IF NOT EXISTS recording_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL UNIQUE,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS recording_holds (
                id TEXT PRIMARY KEY,
                segment_id INTEGER REFERENCES recording_segments(id) ON DELETE SET NULL,
                camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                expires_at TEXT,
                released_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                released_at TEXT,
                CHECK (expires_at IS NULL OR expires_at > created_at)
            );
            CREATE INDEX IF NOT EXISTS recording_holds_active_idx ON recording_holds(segment_id,released_at,expires_at);
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                action TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS audit_log_created_idx ON audit_log(created_at DESC);
            CREATE TABLE IF NOT EXISTS exports (
                id TEXT PRIMARY KEY,
                requested_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                camera_ids_json TEXT NOT NULL,
                requested_start TEXT NOT NULL,
                requested_end TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled')),
                metadata_json TEXT,
                output_relative_path TEXT,
                error TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS exports_owner_created_idx ON exports(requested_by,created_at DESC);
            CREATE INDEX IF NOT EXISTS exports_status_created_idx ON exports(status,created_at);
            CREATE TABLE IF NOT EXISTS export_sources (
                export_id TEXT NOT NULL REFERENCES exports(id) ON DELETE CASCADE,
                segment_id INTEGER NOT NULL REFERENCES recording_segments(id) ON DELETE CASCADE,
                PRIMARY KEY(export_id,segment_id)
            );
            CREATE INDEX IF NOT EXISTS export_sources_segment_idx ON export_sources(segment_id,export_id);
            CREATE INDEX IF NOT EXISTS recording_camera_time_idx ON recording_segments(camera_id, starts_at, ends_at);
            CREATE TABLE IF NOT EXISTS coverage_intervals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
                coverage_kind TEXT NOT NULL CHECK (coverage_kind IN ('covered','expected_idle','failure','uncertain')),
                reason TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT
            );
            CREATE INDEX IF NOT EXISTS coverage_camera_time_idx ON coverage_intervals(camera_id, starts_at DESC);
            CREATE TABLE IF NOT EXISTS motion_events (
                id TEXT PRIMARY KEY,
                camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                source_topic TEXT NOT NULL,
                source_event_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                requested_start TEXT NOT NULL,
                requested_end TEXT NOT NULL,
                actual_start TEXT,
                actual_end TEXT,
                pre_roll_complete INTEGER NOT NULL DEFAULT 0 CHECK (pre_roll_complete IN (0,1)),
                status TEXT NOT NULL CHECK (status IN ('recording','complete','failed')),
                failure_reason TEXT,
                segment_id INTEGER REFERENCES recording_segments(id) ON DELETE SET NULL,
                source_events_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS motion_events_camera_time_idx ON motion_events(camera_id, received_at);
            CREATE TABLE IF NOT EXISTS ai_camera_settings (
                camera_id TEXT PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
                config_override INTEGER NOT NULL DEFAULT 0 CHECK (config_override IN (0,1)),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
                analysis_fps REAL NOT NULL DEFAULT 0.5 CHECK (analysis_fps BETWEEN 0.1 AND 5),
                confidence_threshold REAL NOT NULL DEFAULT 0.55 CHECK (confidence_threshold BETWEEN 0.1 AND 0.99),
                use_substream INTEGER NOT NULL DEFAULT 1 CHECK (use_substream IN (0,1)),
                zones_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'disabled',
                last_attempt_at TEXT,
                last_success_at TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS ai_group_settings (
                group_id TEXT PRIMARY KEY REFERENCES camera_groups(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
                analysis_fps REAL NOT NULL DEFAULT 0.5 CHECK (analysis_fps BETWEEN 0.1 AND 5),
                confidence_threshold REAL NOT NULL DEFAULT 0.55 CHECK (confidence_threshold BETWEEN 0.1 AND 0.99),
                use_substream INTEGER NOT NULL DEFAULT 1 CHECK (use_substream IN (0,1)),
                zones_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS ai_runtime_settings (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
                max_active_cameras INTEGER NOT NULL DEFAULT 4 CHECK (max_active_cameras BETWEEN 1 AND 16),
                provider TEXT NOT NULL DEFAULT 'CPU' CHECK (provider IN ('CPU','GPU')),
                model_id TEXT NOT NULL DEFAULT 'person-vehicle-bike-detection-2004',
                model_version TEXT NOT NULL DEFAULT '2021.4-FP32',
                validated_at TEXT,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            INSERT OR IGNORE INTO ai_runtime_settings(singleton_id,max_active_cameras) VALUES(1,4);
            CREATE TABLE IF NOT EXISTS ai_observations (
                id TEXT PRIMARY KEY,
                camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
                label TEXT NOT NULL CHECK (label IN ('person','vehicle')),
                confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                box_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                frame_width INTEGER NOT NULL,
                frame_height INTEGER NOT NULL,
                model_id TEXT NOT NULL,
                model_version TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'CPU'
            );
            CREATE INDEX IF NOT EXISTS ai_observations_search_idx ON ai_observations(camera_id,label,first_seen_at DESC);
            CREATE TABLE IF NOT EXISTS ai_coverage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
                state TEXT NOT NULL CHECK (state IN ('analyzed','delayed','skipped','failed','unanalyzed')),
                starts_at TEXT NOT NULL,
                ends_at TEXT,
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ai_coverage_camera_time_idx ON ai_coverage(camera_id,starts_at DESC);
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
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY,
                incident_key TEXT NOT NULL UNIQUE,
                camera_id TEXT REFERENCES cameras(id) ON DELETE SET NULL,
                fault_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('active','acknowledged','recovered')),
                fault_active INTEGER NOT NULL CHECK (fault_active IN (0,1)),
                recurrence_count INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                acknowledged_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                acknowledged_at TEXT,
                recovered_at TEXT,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS incidents_state_seen_idx ON incidents(state,last_seen_at DESC);
            CREATE TABLE IF NOT EXISTS incident_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL CHECK (event_type IN ('raised','acknowledged','recovered','recurred')),
                actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                occurred_at TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS incident_events_history_idx ON incident_events(incident_id,id);
            CREATE TABLE IF NOT EXISTS webhook_endpoints (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                target_url_ciphertext BLOB NOT NULL,
                signing_secret_ciphertext BLOB NOT NULL,
                events_json TEXT NOT NULL,
                allow_private INTEGER NOT NULL DEFAULT 0 CHECK (allow_private IN (0,1)),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                id TEXT PRIMARY KEY,
                endpoint_id TEXT REFERENCES webhook_endpoints(id) ON DELETE SET NULL,
                endpoint_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('queued','delivering','delivered','failed')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                response_status INTEGER,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                delivered_at TEXT,
                UNIQUE(endpoint_id,event_id)
            );
            CREATE INDEX IF NOT EXISTS webhook_deliveries_due_idx ON webhook_deliveries(status,next_attempt_at,created_at);
            CREATE INDEX IF NOT EXISTS webhook_deliveries_endpoint_idx ON webhook_deliveries(endpoint_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS heartbeat_configuration (
                singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
                target_url_ciphertext BLOB,
                signing_secret_ciphertext BLOB,
                allow_private INTEGER NOT NULL DEFAULT 0 CHECK (allow_private IN (0,1)),
                interval_seconds INTEGER NOT NULL DEFAULT 60 CHECK (interval_seconds BETWEEN 30 AND 900),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
                instance_id TEXT NOT NULL,
                force_next_test INTEGER NOT NULL DEFAULT 0 CHECK (force_next_test IN (0,1)),
                next_attempt_at TEXT,
                last_attempt_at TEXT,
                last_success_at TEXT,
                last_http_status INTEGER,
                last_error TEXT,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        incident_event_columns = {row[1] for row in db.execute("PRAGMA table_info(incident_events)")}
        if "event_id" not in incident_event_columns:
            db.execute("ALTER TABLE incident_events ADD COLUMN event_id TEXT")
        import uuid
        for row in db.execute("SELECT id FROM incident_events WHERE event_id IS NULL").fetchall():
            db.execute("UPDATE incident_events SET event_id=? WHERE id=?", (str(uuid.uuid4()), row["id"]))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS incident_events_event_id_idx ON incident_events(event_id)")
        ai_columns = {row[1] for row in db.execute("PRAGMA table_info(ai_camera_settings)")}
        if ai_columns and "config_override" not in ai_columns:
            db.execute("ALTER TABLE ai_camera_settings ADD COLUMN config_override INTEGER NOT NULL DEFAULT 0 CHECK (config_override IN (0,1))")
        runtime_columns = {row[1] for row in db.execute("PRAGMA table_info(ai_runtime_settings)")}
        for name, definition in (
            ("provider", "TEXT NOT NULL DEFAULT 'CPU' CHECK (provider IN ('CPU','GPU'))"),
            ("model_id", "TEXT NOT NULL DEFAULT 'person-vehicle-bike-detection-2004'"),
            ("model_version", "TEXT NOT NULL DEFAULT '2021.4-FP32'"),
            ("validated_at", "TEXT"),
        ):
            if name not in runtime_columns:
                db.execute(f"ALTER TABLE ai_runtime_settings ADD COLUMN {name} {definition}")
        observation_columns = {row[1] for row in db.execute("PRAGMA table_info(ai_observations)")}
        if "provider" not in observation_columns:
            db.execute("ALTER TABLE ai_observations ADD COLUMN provider TEXT NOT NULL DEFAULT 'CPU'")
        ai_coverage_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_coverage'").fetchone()[0]
        if "'skipped'" not in ai_coverage_sql:
            db.execute("ALTER TABLE ai_coverage RENAME TO ai_coverage_legacy")
            db.execute("CREATE TABLE ai_coverage (id INTEGER PRIMARY KEY AUTOINCREMENT,camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,state TEXT NOT NULL CHECK (state IN ('analyzed','delayed','skipped','failed','unanalyzed')),starts_at TEXT NOT NULL,ends_at TEXT,detail TEXT NOT NULL DEFAULT '')")
            db.execute("INSERT INTO ai_coverage(id,camera_id,state,starts_at,ends_at,detail) SELECT id,camera_id,state,starts_at,ends_at,detail FROM ai_coverage_legacy")
            db.execute("DROP TABLE ai_coverage_legacy")
            db.execute("CREATE INDEX IF NOT EXISTS ai_coverage_camera_time_idx ON ai_coverage(camera_id,starts_at DESC)")
        # Migrate installations created by the first implementation, which only
        # allowed administrator accounts and did not assign stable session IDs.
        camera_columns = {row[1] for row in db.execute("PRAGMA table_info(cameras)")}
        group_columns = {row[1] for row in db.execute("PRAGMA table_info(camera_groups)")}
        if "storage_volume_id" not in group_columns:
            db.execute("ALTER TABLE camera_groups ADD COLUMN storage_volume_id TEXT")
        if "onvif_uuid" not in camera_columns:
            db.execute("ALTER TABLE cameras ADD COLUMN onvif_uuid TEXT")
        if "group_id" not in camera_columns:
            db.execute("ALTER TABLE cameras ADD COLUMN group_id TEXT REFERENCES camera_groups(id) ON DELETE SET NULL")
        if "live_substream_url_ciphertext" not in camera_columns:
            db.execute("ALTER TABLE cameras ADD COLUMN live_substream_url_ciphertext BLOB")
        if "audio_supported" not in camera_columns:
            db.execute("ALTER TABLE cameras ADD COLUMN audio_supported INTEGER CHECK (audio_supported IN (0,1))")
        if "audio_enabled" not in camera_columns:
            db.execute("ALTER TABLE cameras ADD COLUMN audio_enabled INTEGER NOT NULL DEFAULT 0 CHECK (audio_enabled IN (0,1))")
        for name, definition in (
            ("last_video_at", "TEXT"),
            ("last_footage_write_at", "TEXT"),
            ("recording_fault_since", "TEXT"),
            ("recording_enabled", "INTEGER NOT NULL DEFAULT 0 CHECK (recording_enabled IN (0,1))"),
            ("motion_service_url", "TEXT"),
            ("motion_credentials_ciphertext", "BLOB"),
            ("motion_pre_roll_seconds", "INTEGER NOT NULL DEFAULT 10"),
            ("motion_post_roll_seconds", "INTEGER NOT NULL DEFAULT 30"),
            ("motion_source_status", "TEXT NOT NULL DEFAULT 'untested'"),
            ("motion_fallback_enabled", "INTEGER NOT NULL DEFAULT 0 CHECK (motion_fallback_enabled IN (0,1))"),
            ("motion_zones_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("motion_sensitivity", "INTEGER NOT NULL DEFAULT 50"),
            ("server_motion_status", "TEXT NOT NULL DEFAULT 'disabled'"),
        ):
            if name not in camera_columns:
                db.execute(f"ALTER TABLE cameras ADD COLUMN {name} {definition}")
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
        db.execute(
            "INSERT OR IGNORE INTO camera_group_memberships(group_id,camera_id) "
            "SELECT group_id,id FROM cameras WHERE group_id IS NOT NULL"
        )
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_provider_identity_idx ON users(provider_issuer, provider_subject) WHERE provider_issuer IS NOT NULL AND provider_subject IS NOT NULL")
