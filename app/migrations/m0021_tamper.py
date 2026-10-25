"""Camera tamper detection: per-camera settings and the learned reference, and a ``tamper`` rule condition.

``tamper_settings`` holds, for each camera, whether detection is on, how sensitive it is, how long a change must
last before it counts, the reference the recorder has learned and where the detection stands. ``rules`` is rebuilt
because SQLite cannot change a CHECK constraint in place: the rebuild keeps every row and column, and runs with
foreign keys off, as the schema change procedure SQLite documents requires, so no rule event, alarm or action that
refers to a rule is touched.
"""
from __future__ import annotations

import sqlite3

NAME = "tamper"
TRANSACTIONAL = False

_SETTINGS = """
CREATE TABLE tamper_settings (
    camera_id TEXT PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
    sensitivity INTEGER NOT NULL DEFAULT 50 CHECK (sensitivity BETWEEN 1 AND 100),
    sustain_seconds INTEGER NOT NULL DEFAULT 60 CHECK (sustain_seconds BETWEEN 10 AND 3600),
    reference_json TEXT,
    reference_samples INTEGER NOT NULL DEFAULT 0,
    reference_at TEXT,
    state TEXT NOT NULL DEFAULT 'learning' CHECK (state IN ('learning','ok','covered','moved','defocused')),
    state_since TEXT,
    candidate TEXT,
    candidate_since TEXT,
    clear_samples INTEGER NOT NULL DEFAULT 0,
    last_sample_at TEXT,
    last_metrics_json TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
"""


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(_SETTINGS)
        sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rules'").fetchone()[0]
        if "'tamper'" not in sql:
            rebuilt = sql.replace("CREATE TABLE rules", "CREATE TABLE rules_new", 1).replace("'wrong_way'", "'wrong_way','tamper'", 1)
            if rebuilt == sql:
                raise RuntimeError("The rules table is not in the shape this migration expects")
            indexes = [row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='rules' AND sql IS NOT NULL")]
            db.execute(rebuilt)
            db.execute("INSERT INTO rules_new SELECT * FROM rules")
            db.execute("DROP TABLE rules")
            db.execute("ALTER TABLE rules_new RENAME TO rules")
            for index in indexes:
                db.execute(index)
        broken = db.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"Rebuilding rules left {len(broken)} broken references")
    except BaseException:
        db.execute("ROLLBACK")
        db.execute("PRAGMA foreign_keys = ON")
        raise
    db.execute("COMMIT")
    db.execute("PRAGMA foreign_keys = ON")


def verify(db: sqlite3.Connection) -> None:
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rules'").fetchone()[0]
    if "'tamper'" not in sql:
        raise RuntimeError("The rules table does not accept the tamper condition")
