"""Signed footage: each camera's hash chain of recorded segments, its periodic signatures,
and the installation signing key.

Every closed segment's SHA-256 is appended to its camera's chain with its time range and
the previous link's hash. Removals (retention, administrator deletion, archived camera
footage) are appended too, so expected deletions are not mistaken for tampering. A
trigger queues every removal, whichever code path deletes the segment. Footage indexed
before this migration is "not signed" rather than tampered.
"""
from __future__ import annotations

import sqlite3

NAME = "footage_chain"

_STATEMENTS = (
    """
    CREATE TABLE footage_chain (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        camera_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('segment','removed')),
        segment_id INTEGER NOT NULL,
        relative_path TEXT NOT NULL,
        starts_at TEXT NOT NULL,
        ends_at TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        link_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (camera_id, seq)
    )
    """,
    "CREATE INDEX footage_chain_segment ON footage_chain(segment_id, kind)",
    "CREATE INDEX footage_chain_range ON footage_chain(camera_id, starts_at)",
    """
    CREATE TABLE footage_chain_signatures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        camera_id TEXT NOT NULL,
        upto_seq INTEGER NOT NULL,
        head_hash TEXT NOT NULL,
        signature TEXT NOT NULL,
        public_key TEXT NOT NULL,
        signed_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX footage_chain_signatures_camera ON footage_chain_signatures(camera_id, upto_seq)",
    """
    CREATE TABLE footage_removals (
        segment_id INTEGER PRIMARY KEY,
        camera_id TEXT NOT NULL,
        removed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TRIGGER footage_chain_removal AFTER DELETE ON recording_segments
    BEGIN
        INSERT OR IGNORE INTO footage_removals(segment_id, camera_id) VALUES (OLD.id, OLD.camera_id);
    END
    """,
    """
    CREATE TABLE footage_signing_key (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
        private_key_ciphertext BLOB NOT NULL,
        public_key TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    # Segments up to this id were recorded before footage was signed.
    """
    CREATE TABLE footage_chain_start (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
        after_segment_id INTEGER NOT NULL
    )
    """,
    "INSERT INTO footage_chain_start(singleton_id, after_segment_id) SELECT 1, COALESCE(MAX(id), 0) FROM recording_segments",
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
