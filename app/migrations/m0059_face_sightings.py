"""Face watch (#253), part 3: sightings of watched faces, and the ``face`` rule condition.

A sighting is a watched face seen on a camera: which entry, which camera and person track, when, how alike, where in the picture, and a small picture of the face. Only faces
that matched an entry are ever kept; they go with their track (retention) and with their camera or entry. The rules table is rebuilt to accept the ``face`` condition, as it was
for the earlier conditions.
"""
from __future__ import annotations

import re
import sqlite3

NAME = "face_sightings"
TRANSACTIONAL = False  # The rules table is rebuilt with foreign keys off, which SQLite ignores inside a transaction


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise RuntimeError("Foreign keys could not be switched off for the rules rebuild")
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS face_sightings ("
            "id TEXT PRIMARY KEY, "
            "entry_id TEXT NOT NULL REFERENCES face_watch_entries(id) ON DELETE CASCADE, "
            "camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE, "
            "track_id TEXT NOT NULL REFERENCES ai_tracks(id) ON DELETE CASCADE, "
            "seen_at TEXT NOT NULL, "
            "similarity REAL NOT NULL, "
            "box_json TEXT NOT NULL, "
            "picture BLOB, "
            "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))")
        db.execute("CREATE INDEX IF NOT EXISTS face_sightings_camera_time_idx ON face_sightings(camera_id,seen_at)")
        db.execute("CREATE INDEX IF NOT EXISTS face_sightings_entry_time_idx ON face_sightings(entry_id,seen_at)")
        sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rules'").fetchone()[0]
        if "'face'" not in sql:
            rebuilt = re.sub(r'CREATE TABLE\s+"?rules"?', "CREATE TABLE rules_new", sql, count=1).replace("'inactivity','occupancy'", "'inactivity','occupancy','face'", 1)
            if rebuilt == sql or "rules_new" not in rebuilt or "'face'" not in rebuilt:
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
    if "'face'" not in sql:
        raise RuntimeError("The rules table does not accept the face condition")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='face_sightings'").fetchone() is None:
        raise RuntimeError("face_sightings was not created")
