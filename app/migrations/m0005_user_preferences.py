"""Per-user appearance preference (light, dark or follow the system)."""
from __future__ import annotations

import sqlite3

NAME = "user_preferences"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE users ADD COLUMN theme_preference TEXT NOT NULL DEFAULT 'system' CHECK (theme_preference IN ('system','light','dark'))")
