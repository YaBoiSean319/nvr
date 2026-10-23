from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def database_path() -> Path:
    return Path(os.environ.get("NVR_DB_PATH", "./data/nvr.sqlite3"))


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    return db


def initialize() -> None:
    from app.migrations import migrate

    migrate(database_path(), connect)
