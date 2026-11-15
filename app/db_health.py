"""Is the database file itself damaged? (#321)

A damaged file used to show only as a generic "try again shortly" on every page and a loop of tracebacks in the workers. This module
answers the question directly, for three callers: the nightly self-test (the full ``quick_check``, which reads every page and takes a few
minutes on a large file), a light probe at startup and for the self-test run by hand (it opens the file and reads the start of every
table, which takes milliseconds), and the web error handler, which recognises a damaged file by its error.

Nothing here writes to the database or takes its write lock. The full check holds one read snapshot while it runs, so the write-ahead log
cannot be reset until it finishes; it runs in the background worker, at night.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .store import connect

logger = logging.getLogger(__name__)

RECOVERY_DOCUMENT = "docs/database-recovery.md"
ADVICE = (
    "Stop the NVR and do not delete anything. Copy the data folder, then restore the newest healthy copy "
    f"(the *.pre-migration-* files next to the database, or a configuration backup): see {RECOVERY_DOCUMENT}."
)
_LINES_SHOWN = 5


@dataclass
class Outcome:
    ok: bool
    full: bool
    seconds: float
    lines: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return "; ".join(self.lines[:_LINES_SHOWN]) if self.lines else ("passed" if self.ok else "failed")


def is_damage(error: BaseException) -> bool:
    """A damaged or unreadable file, as opposed to a busy one: SQLite reports corruption and a file that is not a database as a plain
    ``DatabaseError``, while a locked database, a constraint or a bad statement are its subclasses."""
    return type(error) is sqlite3.DatabaseError


def _open(path: Path | str | None) -> sqlite3.Connection:
    if path is None:
        return connect()
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    return db


def check(*, full: bool, path: Path | str | None = None, lines_shown: int = _LINES_SHOWN) -> Outcome:
    """Run the full ``quick_check`` or the light probe on the live database (or on ``path``, a copy)."""
    started = time.monotonic()
    try:
        db = _open(path)
        try:
            if full:
                found = [str(row[0]) for row in db.execute(f"PRAGMA quick_check({int(lines_shown)})").fetchall()]
                lines = [] if found == ["ok"] else found
            else:
                lines = _probe(db)
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        if not is_damage(exc):
            raise  # Busy, out of disk or a bug: not a statement about the file, so the caller decides
        lines = [str(exc)]
    return Outcome(ok=not lines, full=full, seconds=round(time.monotonic() - started, 2), lines=lines)


def _probe(db: sqlite3.Connection) -> list[str]:
    """Open the file and read the first row of every table: finds a file that is not a database, a broken schema and a damaged first page of any table."""
    db.execute("PRAGMA schema_version").fetchone()
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        db.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone()
    return []


def log_at_startup() -> bool:
    """One clear line in the log when the file is damaged, instead of a traceback from every worker. Returns True when the file is fine."""
    try:
        outcome = check(full=False)
    except sqlite3.Error:
        logger.exception("The startup database check could not run")
        return True
    if outcome.ok:
        return True
    logger.error("THE DATABASE FILE IS DAMAGED (%s). %s", outcome.summary, ADVICE)
    return False
