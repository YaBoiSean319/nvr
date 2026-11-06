"""Occupancy rules: let the rules table accept the ``occupancy`` condition (more than a set number of objects in the frame or a zone)."""
from __future__ import annotations

import re
import sqlite3

NAME = "occupancy_rules"
# Rebuilding a table other tables point at needs foreign keys off, which SQLite ignores inside a transaction: this migration runs its own.
TRANSACTIONAL = False


def upgrade(db: sqlite3.Connection) -> None:
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rules'").fetchone()[0]
    if "'occupancy'" in sql:
        return
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            # Dropping rules with this on would delete its actions and detach its events: refuse rather than do that.
            raise RuntimeError("Foreign keys could not be switched off for the rules rebuild")
        db.execute("BEGIN IMMEDIATE")
        # SQLite stores the name quoted ("rules") after an earlier rebuild, so match either spelling.
        rebuilt = re.sub(r'CREATE TABLE\s+"?rules"?', "CREATE TABLE rules_new", sql, count=1).replace("'fall','inactivity'", "'fall','inactivity','occupancy'", 1)
        if rebuilt == sql or "rules_new" not in rebuilt or "'occupancy'" not in rebuilt:
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
    if "'occupancy'" not in sql:
        raise RuntimeError("The rules table does not accept the occupancy condition")
