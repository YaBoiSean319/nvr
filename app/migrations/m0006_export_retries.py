"""Export retries: each retry is a new job that records which job it repeats."""
from __future__ import annotations

import sqlite3

NAME = "export_retries"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE exports ADD COLUMN retry_of TEXT REFERENCES exports(id) ON DELETE SET NULL")
