"""Remote access (#132): an option to require a second factor for sessions that come from outside the local network."""
from __future__ import annotations

import sqlite3

NAME = "remote_second_factor"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE security_settings ADD COLUMN remote_mfa INTEGER NOT NULL DEFAULT 0 CHECK (remote_mfa IN (0,1))")
