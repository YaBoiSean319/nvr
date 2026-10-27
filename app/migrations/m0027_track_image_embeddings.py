"""Image-text embeddings for tracks (natural-language search, #127).

``track_image_embeddings`` keeps one quantized (int8) embedding per track, made from its first sighting by the administrator's
CLIP-family model, with the id of that model: embeddings from different models are never compared. The row goes when its track
goes, so it follows footage retention. The vector is never returned by any API.
"""
from __future__ import annotations

import sqlite3

NAME = "track_image_embeddings"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE track_image_embeddings (
            track_id TEXT PRIMARY KEY REFERENCES ai_tracks(id) ON DELETE CASCADE,
            model_id TEXT NOT NULL,
            dim INTEGER NOT NULL CHECK (dim > 0),
            vector BLOB NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute("CREATE INDEX track_image_embeddings_model_idx ON track_image_embeddings(model_id)")
