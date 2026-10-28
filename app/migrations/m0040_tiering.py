"""Storage tiering (#144): cold tiers (a local path or an S3-compatible bucket), the policy that sends a storage volume's older segments to one, the moves in flight, and
where each tiered segment now lives.

A segment keeps its row, its id and its ``relative_path`` when it moves (the footage chain, holds, exports and cases refer to those); ``tier_id`` says it now lives
on a cold tier under ``tier_key``, and ``tier_sha256`` is the digest it was verified against.
"""
from __future__ import annotations

import sqlite3

NAME = "tiering"

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS cold_tiers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        kind TEXT NOT NULL CHECK (kind IN ('local','s3')),
        local_path TEXT,
        s3_endpoint TEXT,
        s3_bucket TEXT,
        s3_region TEXT NOT NULL DEFAULT 'us-east-1',
        s3_prefix TEXT NOT NULL DEFAULT '',
        credentials_ciphertext BLOB,
        allow_private INTEGER NOT NULL DEFAULT 0 CHECK (allow_private IN (0,1)),
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        last_checked_at TEXT,
        last_error TEXT,
        failures INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tier_policies (
        volume_id TEXT PRIMARY KEY,
        tier_id TEXT NOT NULL REFERENCES cold_tiers(id),
        after_days INTEGER NOT NULL CHECK (after_days >= 1),
        rate_kbps INTEGER NOT NULL DEFAULT 20480 CHECK (rate_kbps >= 64),
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tier_moves (
        segment_id INTEGER PRIMARY KEY,
        tier_id TEXT NOT NULL,
        tier_key TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('copying','switched')),
        sha256 TEXT,
        size_bytes INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tier_orphans (
        tier_id TEXT NOT NULL,
        tier_key TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (tier_id, tier_key)
    )
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    existing = {row[1] for row in db.execute("PRAGMA table_info(recording_segments)")}
    for name in ("tier_id", "tier_key", "tier_sha256", "tiered_at"):
        if name not in existing:
            db.execute(f"ALTER TABLE recording_segments ADD COLUMN {name} TEXT")
    for statement in _STATEMENTS:
        db.execute(statement)
    db.execute("CREATE INDEX IF NOT EXISTS recording_segments_tier_idx ON recording_segments(tier_id) WHERE tier_id IS NOT NULL")


def verify(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(recording_segments)")}
    if not {"tier_id", "tier_key", "tier_sha256", "tiered_at"} <= columns:
        raise RuntimeError("recording_segments has no tier columns")
