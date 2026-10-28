"""PTZ auto-tracking (#145): who is being followed on which camera, and the per-camera settings.

A tracking lease is an ordinary PTZ lease (#114) with ``purpose='tracking'``: the person (or the rule's author) who started tracking holds it while the server
steers the camera, and anybody's manual command replaces it and ends the tracking. ``rule_actions`` is rebuilt to accept the ``ptz_track`` action, because SQLite
cannot change a CHECK in place.
"""
from __future__ import annotations

import re
import sqlite3

NAME = "ptz_tracking"
TRANSACTIONAL = False

_STATEMENTS = (
    "ALTER TABLE ptz_leases ADD COLUMN purpose TEXT NOT NULL DEFAULT 'manual' CHECK (purpose IN ('manual','tracking'))",
    """
    CREATE TABLE ptz_tracking (
        camera_id TEXT PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
        track_id TEXT NOT NULL,
        label TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('operator','rule')),
        lease_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        started_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        rule_id TEXT,
        started_at TEXT NOT NULL,
        last_detection_at TEXT,
        last_seen_at TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'tracking' CHECK (state IN ('tracking','lost')),
        lost_since TEXT
    )
    """,
    """
    CREATE TABLE ptz_tracking_settings (
        camera_id TEXT PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
        home_preset_id TEXT REFERENCES ptz_presets(id) ON DELETE SET NULL,
        lost_seconds REAL NOT NULL DEFAULT 5 CHECK (lost_seconds BETWEEN 1 AND 60),
        home_timeout_seconds INTEGER NOT NULL DEFAULT 30 CHECK (home_timeout_seconds BETWEEN 5 AND 3600),
        target_size REAL NOT NULL DEFAULT 0.4 CHECK (target_size BETWEEN 0.1 AND 0.9),
        zoom_enabled INTEGER NOT NULL DEFAULT 1 CHECK (zoom_enabled IN (0,1)),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            # Dropping rule_actions with this on would detach its deliveries: refuse rather than do that.
            raise RuntimeError("Foreign keys could not be switched off for the rule actions rebuild")
        db.execute("BEGIN IMMEDIATE")
        if "purpose" not in {row[1] for row in db.execute("PRAGMA table_info(ptz_leases)")}:
            db.execute(_STATEMENTS[0])
        for statement in _STATEMENTS[1:]:
            db.execute(statement.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
        sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rule_actions'").fetchone()[0]
        if "'ptz_track'" not in sql:
            rebuilt = re.sub(r'CREATE TABLE\s+"?rule_actions"?', "CREATE TABLE rule_actions_new", sql, count=1).replace("'relay'", "'relay','ptz_track'", 1)
            if rebuilt == sql or "rule_actions_new" not in rebuilt or "'ptz_track'" not in rebuilt:
                raise RuntimeError("The rule_actions table is not in the shape this migration expects")
            indexes = [row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='rule_actions' AND sql IS NOT NULL")]
            db.execute(rebuilt)
            db.execute("INSERT INTO rule_actions_new SELECT * FROM rule_actions")
            db.execute("DROP TABLE rule_actions")
            db.execute("ALTER TABLE rule_actions_new RENAME TO rule_actions")
            for index in indexes:
                db.execute(index)
        broken = [row for row in db.execute("PRAGMA foreign_key_check") if row[2] == "rule_actions"]
        if broken:
            raise RuntimeError(f"Rebuilding rule_actions left {len(broken)} broken references to it")
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")


def verify(db: sqlite3.Connection) -> None:
    if "purpose" not in {row[1] for row in db.execute("PRAGMA table_info(ptz_leases)")}:
        raise RuntimeError("ptz_leases has no purpose column")
    if "'ptz_track'" not in db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rule_actions'").fetchone()[0]:
        raise RuntimeError("rule_actions does not accept the ptz_track action")
    for table in ("ptz_tracking", "ptz_tracking_settings"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            raise RuntimeError(f"{table} was not created")
