"""Email alerts through Microsoft sign-in (#231): the one connection to a Microsoft 365 mailbox, and a rule action that sends email.

``email_connection`` holds the tenant and app, the signed-in account, and two encrypted blobs: the pending device sign-in and the tokens. ``rule_actions`` is
rebuilt to accept the ``email`` action, because SQLite cannot change a CHECK in place.
"""
from __future__ import annotations

import re
import sqlite3

NAME = "email_alerts"
TRANSACTIONAL = False

_TABLE = """
CREATE TABLE IF NOT EXISTS email_connection (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    tenant_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    sender TEXT,
    account TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending','connected','error')),
    pending_ciphertext BLOB,
    tokens_ciphertext BLOB,
    last_error TEXT,
    connected_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
"""


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise RuntimeError("Foreign keys could not be switched off for the rule actions rebuild")
        db.execute("BEGIN IMMEDIATE")
        db.execute(_TABLE)
        sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rule_actions'").fetchone()[0]
        if "'email'" not in sql:
            rebuilt = re.sub(r'CREATE TABLE\s+"?rule_actions"?', "CREATE TABLE rule_actions_new", sql, count=1).replace("'ptz_track'", "'ptz_track','email'", 1)
            if rebuilt == sql or "rule_actions_new" not in rebuilt or "'email'" not in rebuilt:
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
    if "'email'" not in db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rule_actions'").fetchone()[0]:
        raise RuntimeError("rule_actions does not accept the email action")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_connection'").fetchone() is None:
        raise RuntimeError("email_connection was not created")
