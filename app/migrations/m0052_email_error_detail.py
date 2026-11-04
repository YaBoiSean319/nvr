"""Email alerts (#231): keep what Microsoft said when it refused something (its error code and message), so the settings page can show the real reason."""
from __future__ import annotations

import sqlite3

NAME = "email_error_detail"


def upgrade(db: sqlite3.Connection) -> None:
    if "last_detail" not in {row[1] for row in db.execute("PRAGMA table_info(email_connection)")}:
        db.execute("ALTER TABLE email_connection ADD COLUMN last_detail TEXT")


def verify(db: sqlite3.Connection) -> None:
    if "last_detail" not in {row[1] for row in db.execute("PRAGMA table_info(email_connection)")}:
        raise RuntimeError("email_connection has no last_detail")
