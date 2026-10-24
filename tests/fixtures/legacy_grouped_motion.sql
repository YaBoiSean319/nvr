-- Database produced by the unversioned schema at commit 3d27933, with synthetic fixture rows.
BEGIN TRANSACTION;
CREATE TABLE camera_groups (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
INSERT INTO "camera_groups" VALUES('group-lobby','Lobby','2026-01-01T00:00:00.000Z');
CREATE TABLE cameras (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                onvif_uuid TEXT,
                group_id TEXT REFERENCES camera_groups(id) ON DELETE SET NULL,
                stream_url_ciphertext BLOB NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
                recording_mode TEXT NOT NULL DEFAULT 'motion' CHECK (recording_mode IN ('motion','continuous','disabled')),
                recording_enabled INTEGER NOT NULL DEFAULT 0 CHECK (recording_enabled IN (0,1)),
                recording_status TEXT NOT NULL DEFAULT 'stopped',
                motion_service_url TEXT,
                motion_credentials_ciphertext BLOB,
                motion_pre_roll_seconds INTEGER NOT NULL DEFAULT 10,
                motion_post_roll_seconds INTEGER NOT NULL DEFAULT 30,
                motion_source_status TEXT NOT NULL DEFAULT 'untested',
                video_codec TEXT,
                video_width INTEGER,
                video_height INTEGER,
                last_probe_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
INSERT INTO "cameras" VALUES('camera-a','Fixture Camera A',NULL,'group-lobby',X'00',1,'motion',1,'idle',NULL,NULL,10,30,'untested',NULL,NULL,NULL,NULL,NULL,'2026-01-01T00:00:00.000Z','2026-01-01T00:00:00.000Z');
CREATE TABLE diagnostic_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                event_type TEXT NOT NULL,
                fields_json TEXT NOT NULL
            );
CREATE TABLE diagnostic_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
CREATE TABLE discovery_candidates (
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
CREATE TABLE motion_events (
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
CREATE TABLE recording_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL UNIQUE,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
INSERT INTO "recording_segments" VALUES(1,'camera-a','camera-a/2026/01/01/000000.mp4','2026-01-01T00:00:00.000Z','2026-01-01T00:01:00.000Z',1024,'2026-01-01T00:01:00.000Z');
CREATE TABLE sessions (
                session_id TEXT NOT NULL UNIQUE,
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
INSERT INTO "sessions" VALUES('0123456789abcdef0123456789abcdef','fixture-token-hash',1,'2026-01-02T00:00:00.000Z','2026-01-01T00:00:00.000Z');
CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('administrator','operator')),
                provider_issuer TEXT,
                provider_subject TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
INSERT INTO "users" VALUES(1,'fixture-admin','scrypt$fixture-not-a-real-hash','administrator',NULL,NULL,'2026-01-01T00:00:00.000Z');
INSERT INTO "users" VALUES(2,'fixture-operator','scrypt$fixture-not-a-real-hash','operator',NULL,NULL,'2026-01-01T00:00:00.000Z');
CREATE INDEX sessions_expiry_idx ON sessions(expires_at);
CREATE INDEX recording_camera_time_idx ON recording_segments(camera_id, starts_at, ends_at);
CREATE INDEX motion_events_camera_time_idx ON motion_events(camera_id, received_at);
CREATE INDEX diagnostic_events_created_idx ON diagnostic_events(created_at);
CREATE UNIQUE INDEX cameras_onvif_uuid_idx ON cameras(onvif_uuid) WHERE onvif_uuid IS NOT NULL;
CREATE UNIQUE INDEX users_provider_identity_idx ON users(provider_issuer, provider_subject) WHERE provider_issuer IS NOT NULL AND provider_subject IS NOT NULL;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('users',2);
INSERT INTO "sqlite_sequence" VALUES('recording_segments',1);
COMMIT;
