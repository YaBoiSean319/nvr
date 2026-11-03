"""Fall detection (#230): the cameras it watches (with sensitivity and how long a person must stay down), the falls found, and a ``fall`` rule condition.

A fall goes with the footage it was seen in: when that footage is deleted (retention, or the headroom) the fall record goes too. ``rules`` is rebuilt, as for
the plate, tamper and external-event conditions, because SQLite cannot change a CHECK in place.
"""
from __future__ import annotations

import re
import sqlite3

NAME = "falls"
TRANSACTIONAL = False

_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS fall_settings (
        camera_id TEXT PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
        sensitivity TEXT NOT NULL DEFAULT 'high' CHECK (sensitivity IN ('low','normal','high')),
        stay_down_seconds REAL NOT NULL DEFAULT 3 CHECK (stay_down_seconds >= 1 AND stay_down_seconds <= 60),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fall_events (
        id TEXT PRIMARY KEY,
        camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        track_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        down_seconds REAL NOT NULL,
        box_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS fall_events_camera_time_idx ON fall_events(camera_id,occurred_at)",
    """
    CREATE TRIGGER IF NOT EXISTS recording_segments_remove_fall_events AFTER DELETE ON recording_segments BEGIN
        DELETE FROM fall_events
        WHERE camera_id=OLD.camera_id AND occurred_at>=OLD.starts_at AND occurred_at<OLD.ends_at
          AND NOT EXISTS (
              SELECT 1 FROM recording_segments s
              WHERE s.camera_id=OLD.camera_id AND s.starts_at<=fall_events.occurred_at AND s.ends_at>fall_events.occurred_at
          );
    END
    """,
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
        if "'fall'" not in sql:
            # After an earlier rebuild SQLite stores the name quoted ("rules"), so match either spelling.
            rebuilt = re.sub(r'CREATE TABLE\s+"?rules"?', "CREATE TABLE rules_new", sql, count=1).replace("'external_event','plate'", "'external_event','plate','fall'", 1)
            if rebuilt == sql or "rules_new" not in rebuilt or "'fall'" not in rebuilt:
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
    if "'fall'" not in sql:
        raise RuntimeError("The rules table does not accept the fall condition")
