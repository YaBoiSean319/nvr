"""Licence plates (#142): the cameras designated for reading plates, the reads (with their track, confidence and a crop), watch lists and their entries,
and a ``plate`` rule condition.

A read belongs to a track and goes with it: when a track is deleted with its footage the reads, and their crops, go too. ``folded`` is the plate with
look-alike characters (O and 0, I and 1, ...) made one, so a search can forgive what a camera commonly gets wrong. ``rules`` is rebuilt, as for the
tamper and external-event conditions, because SQLite cannot change a CHECK in place.
"""
from __future__ import annotations

import re
import sqlite3

NAME = "plates"
TRANSACTIONAL = False

_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS plate_settings (
        camera_id TEXT PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
        min_confidence REAL NOT NULL DEFAULT 0.6 CHECK (min_confidence >= 0 AND min_confidence <= 1),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plate_reads (
        id TEXT PRIMARY KEY,
        camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        track_id TEXT NOT NULL REFERENCES ai_tracks(id) ON DELETE CASCADE,
        plate TEXT NOT NULL,
        folded TEXT NOT NULL,
        confidence REAL NOT NULL,
        read_at TEXT NOT NULL,
        box_json TEXT NOT NULL,
        crop BLOB,
        reader TEXT NOT NULL,
        UNIQUE (track_id, plate)
    )
    """,
    "CREATE INDEX IF NOT EXISTS plate_reads_camera_time_idx ON plate_reads(camera_id,read_at)",
    "CREATE INDEX IF NOT EXISTS plate_reads_plate_idx ON plate_reads(plate)",
    "CREATE INDEX IF NOT EXISTS plate_reads_folded_idx ON plate_reads(folded)",
    # A read goes with the footage it was read from, even while its track lives on in newer footage.
    """
    CREATE TRIGGER IF NOT EXISTS recording_segments_remove_plate_reads AFTER DELETE ON recording_segments BEGIN
        DELETE FROM plate_reads
        WHERE camera_id=OLD.camera_id AND read_at>=OLD.starts_at AND read_at<OLD.ends_at
          AND NOT EXISTS (
              SELECT 1 FROM recording_segments s
              WHERE s.camera_id=OLD.camera_id AND s.starts_at<=plate_reads.read_at AND s.ends_at>plate_reads.read_at
          );
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS plate_watch_lists (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        kind TEXT NOT NULL CHECK (kind IN ('allow','deny')),
        lookalikes INTEGER NOT NULL DEFAULT 0 CHECK (lookalikes IN (0,1)),
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plate_watch_entries (
        id TEXT PRIMARY KEY,
        list_id TEXT NOT NULL REFERENCES plate_watch_lists(id) ON DELETE CASCADE,
        pattern TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        expires_at TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        UNIQUE (list_id, pattern)
    )
    """,
    "CREATE INDEX IF NOT EXISTS plate_watch_entries_list_idx ON plate_watch_entries(list_id)",
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
        if "'plate'" not in sql:
            # After an earlier rebuild SQLite stores the name quoted ("rules"), so match either spelling.
            rebuilt = re.sub(r'CREATE TABLE\s+"?rules"?', "CREATE TABLE rules_new", sql, count=1).replace("'external_event'", "'external_event','plate'", 1)
            if rebuilt == sql or "rules_new" not in rebuilt or "'plate'" not in rebuilt:
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
    if "'plate'" not in sql:
        raise RuntimeError("The rules table does not accept the plate condition")
