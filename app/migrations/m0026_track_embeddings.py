"""Appearance embeddings for tracks, for finding the same-looking person or vehicle on other cameras.

``track_embeddings`` keeps one quantized (int8) embedding for each track, made from the track's keyframe, with the id of the model
that made it: embeddings from different models are never compared. The row goes when its track goes, so embeddings follow footage
retention (a track is deleted with its last detection). The vector is never returned by any API.
"""
from __future__ import annotations

import sqlite3

NAME = "track_embeddings"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE track_embeddings (
            track_id TEXT PRIMARY KEY REFERENCES ai_tracks(id) ON DELETE CASCADE,
            model_id TEXT NOT NULL,
            dim INTEGER NOT NULL CHECK (dim > 0),
            vector BLOB NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute("CREATE INDEX track_embeddings_model_idx ON track_embeddings(model_id)")
