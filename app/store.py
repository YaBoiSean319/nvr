from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def database_path() -> Path:
    return Path(os.environ.get("NVR_DB_PATH", "./data/nvr.sqlite3"))


class _Connection(sqlite3.Connection):
    """``with connect() as db`` commits or rolls back and then closes. sqlite3's own context manager leaves the connection open until it
    is garbage collected, so every request left two to four of them behind (#278)."""

    def __exit__(self, *exc_info: object) -> bool | None:
        try:
            return super().__exit__(*exc_info)
        finally:
            self.close()


_prepared: set[str] = set()


def _synchronous() -> str:
    # WAL with NORMAL never corrupts the database; a power cut can lose the last few commits, where FULL waits for the disk on every one.
    value = os.environ.get("NVR_DB_SYNCHRONOUS", "NORMAL").strip().upper()
    return value if value in {"OFF", "NORMAL", "FULL", "EXTRA"} else "NORMAL"


def connect() -> sqlite3.Connection:
    path = database_path()
    key = str(path)
    if key not in _prepared or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level=None, factory=_Connection)
    if key not in _prepared or not path.exists():
        os.chmod(path, 0o600)
        _prepared.add(key)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    db.execute(f"PRAGMA synchronous = {_synchronous()}")
    return db


def initialize() -> None:
    from app.migrations import migrate

    migrate(database_path(), connect)
