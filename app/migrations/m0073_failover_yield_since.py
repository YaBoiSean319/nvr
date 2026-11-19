"""A primary remembers since when it has been yielding to its standby (#343).

A primary that is told "not yet" stops recording for a lease and asks again. Nothing bounded how long that could go on, so a standby that
never handed back (or anything able to keep answering in its place) kept the primary from recording for ever, with no incident: the time
was counted as expected. ``yield_since`` is when the present run of "not yet" answers began; past a limit the primary records again.
"""
from __future__ import annotations

import sqlite3

NAME = "failover_yield_since"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE failover_state ADD COLUMN yield_since TEXT")
