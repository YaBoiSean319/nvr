"""External events in (#138): sources, the mapping of their events to cameras, the events themselves, Home Assistant, and an ``external_event`` rule condition.

A source is a way events reach the NVR: a signed HTTP endpoint (with its own secret) or an MQTT subscription (broker address, topic filter), or the
Home Assistant event topic. Secrets and broker logins are encrypted like camera credentials and never returned. A mapping says which cameras an event
about a subject (a door, a sensor, a button) belongs to. ``rules`` is rebuilt, as for the tamper condition, because SQLite cannot change a CHECK in place.
"""
from __future__ import annotations

import re
import sqlite3

NAME = "external_events"
TRANSACTIONAL = False

_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS event_sources (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('http','mqtt','home_assistant')),
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
        secret_ciphertext BLOB,
        settings_json TEXT NOT NULL DEFAULT '{}',
        target TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        last_event_at TEXT,
        last_error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_mappings (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES event_sources(id) ON DELETE CASCADE,
        subject TEXT NOT NULL,
        types_json TEXT NOT NULL DEFAULT '[]',
        camera_ids_json TEXT NOT NULL,
        UNIQUE (source_id, subject)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS external_events (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES event_sources(id) ON DELETE CASCADE,
        source_name TEXT NOT NULL,
        event_id TEXT NOT NULL,
        type TEXT NOT NULL,
        subject TEXT NOT NULL DEFAULT '',
        message TEXT NOT NULL DEFAULT '',
        data_json TEXT NOT NULL DEFAULT '{}',
        occurred_at TEXT NOT NULL,
        received_at TEXT NOT NULL,
        UNIQUE (source_id, event_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS external_events_time_idx ON external_events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS external_events_received_idx ON external_events(received_at)",
    """
    CREATE TABLE IF NOT EXISTS external_event_cameras (
        event_id TEXT NOT NULL REFERENCES external_events(id) ON DELETE CASCADE,
        camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        PRIMARY KEY (event_id, camera_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS external_event_cameras_camera_idx ON external_event_cameras(camera_id)",
    """
    CREATE TABLE IF NOT EXISTS home_assistant_settings (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
        enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
        broker_ciphertext BLOB,
        allow_private INTEGER NOT NULL DEFAULT 0 CHECK (allow_private IN (0,1)),
        discovery_prefix TEXT NOT NULL DEFAULT 'homeassistant',
        node_id TEXT NOT NULL DEFAULT 'nvr',
        events_topic TEXT NOT NULL DEFAULT 'nvr/events',
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "INSERT OR IGNORE INTO home_assistant_settings(singleton_id) VALUES(1)",
)


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            # Dropping rules with this on would delete its actions and detach its events: refuse rather than do that.
            raise RuntimeError("Foreign keys could not be switched off for the rules rebuild")
        db.execute("BEGIN IMMEDIATE")
        for statement in _TABLES:
            db.execute(statement)
        sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rules'").fetchone()[0]
        if "'external_event'" not in sql:
            # After an earlier rebuild SQLite stores the name quoted ("rules"), so match either spelling.
            rebuilt = re.sub(r'CREATE TABLE\s+"?rules"?', "CREATE TABLE rules_new", sql, count=1).replace("'tamper'", "'tamper','external_event'", 1)
            if rebuilt == sql or "rules_new" not in rebuilt or "'external_event'" not in rebuilt:
                raise RuntimeError("The rules table is not in the shape this migration expects")
            indexes = [row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='rules' AND sql IS NOT NULL")]
            db.execute(rebuilt)
            db.execute("INSERT INTO rules_new SELECT * FROM rules")
            db.execute("DROP TABLE rules")
            db.execute("ALTER TABLE rules_new RENAME TO rules")
            for index in indexes:
                db.execute(index)
        broken = [row for row in db.execute("PRAGMA foreign_key_check") if row[2] == "rules"]
        if broken:
            raise RuntimeError(f"Rebuilding rules left {len(broken)} broken references to it")
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")


def verify(db: sqlite3.Connection) -> None:
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rules'").fetchone()[0]
    if "'external_event'" not in sql:
        raise RuntimeError("The rules table does not accept the external_event condition")
