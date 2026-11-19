"""Viewing audit: an index over the sessions still open (#386).

The pass that ends sessions a crash left open now runs every few minutes, not only when the recorder starts. Open sessions are a handful
among a table that grows for the whole retention period, so they get an index of their own.
"""
from __future__ import annotations

import sqlite3

NAME = "viewing_audit_open_index"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS viewing_audit_open_idx ON viewing_audit(last_seen_at) WHERE ended_at IS NULL")
