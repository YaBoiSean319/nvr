-- Database produced by the unversioned schema at commit 2ccad32, with synthetic fixture rows.
BEGIN TRANSACTION;
CREATE TABLE cameras (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
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
INSERT INTO "cameras" VALUES('camera-a','Fixture Camera A',X'00',1,'continuous','idle',NULL,NULL,NULL,NULL,NULL,'2026-01-01T00:00:00.000Z','2026-01-01T00:00:00.000Z');
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
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
INSERT INTO "sessions" VALUES('fixture-token-hash',1,'2026-01-02T00:00:00.000Z','2026-01-01T00:00:00.000Z');
CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('administrator')),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
INSERT INTO "users" VALUES(1,'fixture-admin','scrypt$fixture-not-a-real-hash','administrator','2026-01-01T00:00:00.000Z');
CREATE INDEX sessions_expiry_idx ON sessions(expires_at);
CREATE INDEX recording_camera_time_idx ON recording_segments(camera_id, starts_at, ends_at);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('users',1);
INSERT INTO "sqlite_sequence" VALUES('recording_segments',1);
COMMIT;
