"""Accounts can be disabled (#364).

There was no way to take a person's access away: an Entra account whose role was removed kept its open session, its device sign-ins, its API
tokens and its alarm notifications, and the Users page offered nothing. ``disabled_at`` marks an account an administrator switched off; every
way of proving who you are refuses a disabled account.

``device_sessions.proved_at`` is when the person last really signed in behind a device sign-in. A device sign-in extends itself each time it is
used, and one can be made from a session another produced, so for an Entra account nothing ever asked Entra again. With the time kept, an Entra
account's device sign-in stops after a set number of days and the person signs in with Entra once more. Rows from before count from when they
were made.
"""
from __future__ import annotations

import sqlite3

NAME = "account_disabled"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE users ADD COLUMN disabled_at TEXT")
    db.execute("ALTER TABLE device_sessions ADD COLUMN proved_at TEXT")
