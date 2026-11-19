"""A single export remembers the SHA-256 of the package it issued (#363).

Verifying an export checked the uploaded package against the list of file hashes inside the package, which nothing signs: a replaced video
with a rewritten list verified. Case packages already keep the hash of the file the NVR issued and compare against it; exports now do too.
Exports made before this have none and are reported as "issued hash not recorded".
"""
from __future__ import annotations

import sqlite3

NAME = "export_package_hash"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE exports ADD COLUMN package_sha256 TEXT")
