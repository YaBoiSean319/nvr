"""Failover recorder (#143): the role and peer of this NVR, the lease and takeover state, the takeover episodes, and the footage imported on handback.

``recording_segments.source`` gains ``failover`` (footage a standby recorded while the primary was down, imported into the primary's index on handback), which
SQLite can only do by rebuilding the table: its indexes and triggers are kept (read from the schema and re-created) and the id sequence continues where it was.
"""
from __future__ import annotations

import re
import sqlite3

NAME = "failover"
TRANSACTIONAL = False

_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS failover_settings (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
        role TEXT NOT NULL DEFAULT 'off' CHECK (role IN ('off','primary','standby')),
        peer_url TEXT,
        secret_ciphertext BLOB,
        allow_private INTEGER NOT NULL DEFAULT 0 CHECK (allow_private IN (0,1)),
        silence_seconds INTEGER NOT NULL DEFAULT 120 CHECK (silence_seconds BETWEEN 10 AND 3600),
        lease_seconds INTEGER NOT NULL DEFAULT 60 CHECK (lease_seconds BETWEEN 5 AND 1800),
        sync_seconds INTEGER NOT NULL DEFAULT 900 CHECK (sync_seconds BETWEEN 60 AND 86400),
        instance_id TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS failover_state (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
        -- Primary: until when its lease holds; until when it must not record (it is yielding, or waiting to hear from the standby); the last answer it had.
        lease_until TEXT,
        yield_until TEXT,
        last_contact_at TEXT,
        peer_state TEXT,
        -- Standby: the last renewal it received, whether it is recording in the primary's place, since when, and the number of takeovers so far (a fence).
        last_renewed_at TEXT,
        handback_requested_at TEXT,
        watching_since TEXT,
        takeover_active INTEGER NOT NULL DEFAULT 0 CHECK (takeover_active IN (0,1)),
        takeover_started_at TEXT,
        epoch INTEGER NOT NULL DEFAULT 0,
        last_sync_at TEXT,
        last_sync_error TEXT,
        sync_requested INTEGER NOT NULL DEFAULT 0 CHECK (sync_requested IN (0,1)),
        config_created_at TEXT,
        last_error TEXT,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "INSERT OR IGNORE INTO failover_state(singleton_id) VALUES(1)",
    """
    CREATE TABLE IF NOT EXISTS failover_episodes (
        id TEXT PRIMARY KEY,
        epoch INTEGER NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('primary','standby')),
        silent_since TEXT,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        state TEXT NOT NULL CHECK (state IN ('active','handback','done')),
        segments_imported INTEGER NOT NULL DEFAULT 0,
        bytes_imported INTEGER NOT NULL DEFAULT 0,
        detail TEXT,
        UNIQUE (epoch, role)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS failover_imports (
        epoch INTEGER NOT NULL,
        remote_segment_id INTEGER NOT NULL,
        segment_id INTEGER,
        camera_id TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (epoch, remote_segment_id)
    )
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            # Dropping recording_segments with this on would cascade into everything that refers to it: refuse rather than do that.
            raise RuntimeError("Foreign keys could not be switched off for the recording_segments rebuild")
        db.execute("BEGIN IMMEDIATE")
        for statement in _TABLES:
            db.execute(statement)
        db.execute("INSERT OR IGNORE INTO failover_settings(singleton_id,instance_id) VALUES(1,lower(hex(randomblob(16))))")
        sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='recording_segments'").fetchone()[0]
        if "'failover'" not in sql:
            rebuilt = re.sub(r'CREATE TABLE\s+"?recording_segments"?', "CREATE TABLE recording_segments_new", sql, count=1).replace("'live','backfill'", "'live','backfill','failover'", 1)
            if rebuilt == sql or "recording_segments_new" not in rebuilt or "'failover'" not in rebuilt:
                raise RuntimeError("The recording_segments table is not in the shape this migration expects")
            kept = [row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='recording_segments' AND type IN ('index','trigger') AND sql IS NOT NULL")]
            sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='recording_segments'").fetchone()
            db.execute(rebuilt)
            db.execute("INSERT INTO recording_segments_new SELECT * FROM recording_segments")
            db.execute("DROP TABLE recording_segments")
            db.execute("ALTER TABLE recording_segments_new RENAME TO recording_segments")
            if sequence is not None:
                db.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='recording_segments'", (sequence[0],))
            for statement in kept:
                db.execute(statement)
        broken = [row for row in db.execute("PRAGMA foreign_key_check") if row[2] == "recording_segments"]
        if broken:
            raise RuntimeError(f"Rebuilding recording_segments left {len(broken)} broken references to it")
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")


def verify(db: sqlite3.Connection) -> None:
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='recording_segments'").fetchone()[0]
    if "'failover'" not in sql:
        raise RuntimeError("The recording_segments table does not accept failover footage")
    triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='recording_segments'")}
    missing = {"recording_segments_remove_detections", "footage_chain_removal", "recording_segments_remove_plate_reads"} - triggers
    if missing:
        raise RuntimeError(f"The recording_segments rebuild lost triggers: {sorted(missing)}")
