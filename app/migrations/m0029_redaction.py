"""Export redaction (#130): reviews of the faces and plates found in a range, and the export that used one."""
from __future__ import annotations

import sqlite3

NAME = "redaction"


_STATEMENTS = (
    """
    CREATE TABLE redaction_reviews (
        id TEXT PRIMARY KEY,
        requested_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        camera_ids_json TEXT NOT NULL,
        requested_start TEXT NOT NULL,
        requested_end TEXT NOT NULL,
        kinds_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('queued','analysing','ready','confirmed','used','failed')),
        progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
        error TEXT,
        regions_json TEXT NOT NULL DEFAULT '{}',
        models_json TEXT NOT NULL DEFAULT '{}',
        edited_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        edited_at TEXT,
        confirmed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        confirmed_at TEXT,
        export_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX redaction_reviews_state_idx ON redaction_reviews(state,created_at)",
    "ALTER TABLE exports ADD COLUMN redaction_review_id TEXT",
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
