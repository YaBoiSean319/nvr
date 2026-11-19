"""Makes the test suite cheaper to run without changing what it checks. Nothing here is part of the server: the image does not hold ``tests/``.

Measured over the whole suite, two fixed costs were a third of its time, paid again by almost every test:

* **Password hashing** (a fifth of the time). The server hashes with 600,000 rounds of PBKDF2 so that a stolen database is slow to guess; a test that
  makes three accounts and signs each in pays that six times. Tests hash with ``TEST_PASSWORD_ROUNDS``. The format, the salt and the comparison are the
  same code, and a stored hash carries its own round count, so verification is unchanged. ``tests/test_suite_speed.py`` pins the server's real figure.
* **Building an empty database** (an eighth). Each test starts from a new file and replayed all the schema migrations to get there. The first new
  database in a process is still built that way, by the real migrations; later ones start as a copy of it. A database that already exists (an upgrade,
  a damaged file, a restart) always goes through the real migrations, and so does any run whose list of migrations differs from the one copied.

One more thing here is about dates, not speed:

* **The recorder's background retention pass does not run by itself in tests.** Many tests give a camera footage with a fixed date in
  October 2026 and then run the whole application, whose recorder starts a retention pass at once. That pass rightly deletes footage older
  than the 30-day default, so from 19 November 2026 at 10:00 UTC those tests began losing their footage in the middle of a test (23 failures
  in ``tests.test_redaction`` on that day, with more files due the next). Nothing asserted on the background pass: retention is tested by
  calling ``cleanup_expired_recordings`` with a clock of the test's choosing. ``tests/test_suite_speed.py`` pins that the server's real loop
  is what the recorder starts outside the tests.

``python -m unittest tests.<module>`` and ``python -m unittest discover -s tests -t .`` import this package first. A run that does not (``discover -s tests``
without ``-t .``) is still correct, only slower for the modules that never import ``tests``.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import app.migrations as _migrations
import app.security as _security

PRODUCTION_PASSWORD_ROUNDS = _security._ITERATIONS
TEST_PASSWORD_ROUNDS = 1_000
_security._ITERATIONS = TEST_PASSWORD_ROUNDS

_real_migrate = _migrations.migrate
_empty_databases: dict[tuple, tuple[bytes, list[int]]] = {}
_copying = True


def _fresh_identity(db: sqlite3.Connection) -> None:
    """What a migration makes at random for each installation must not be shared by every copy: two sites in one test are two installations."""
    db.execute("UPDATE failover_settings SET instance_id=lower(hex(randomblob(16))) WHERE singleton_id=1")


def _migrate(path: Path, connect: Callable[[], sqlite3.Connection]) -> list[int]:
    path = Path(path)
    if not _copying or path.exists():
        return _real_migrate(path, connect)
    key = tuple(_migrations.MIGRATIONS)
    if key not in _empty_databases:
        done = _real_migrate(path, connect)
        with connect() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _empty_databases[key] = (path.read_bytes(), done)
        return done
    content, done = _empty_databases[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
    _real_migrate(path, connect)  # Finds nothing left to apply, as on any restart
    with connect() as db:
        _fresh_identity(db)
    return list(done)


_migrations.migrate = _migrate


import app.recording as _recording  # noqa: E402 - after the migrations are wrapped

REAL_RETENTION_LOOP = _recording.RecordingManager._retention_loop


async def _no_background_retention(self: "_recording.RecordingManager") -> None:
    """In tests the recorder's periodic retention, viewing-audit and disk pass waits to be called for (see the module docstring)."""
    return None


_recording.RecordingManager._retention_loop = _no_background_retention


@contextmanager
def real_migrations() -> Iterator[None]:
    """Every new database inside this block is built by replaying the migrations, as on a server."""
    global _copying
    previous, _copying = _copying, False
    try:
        yield
    finally:
        _copying = previous
