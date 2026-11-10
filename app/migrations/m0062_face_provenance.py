"""Where in the picture each enrolled face was (#301).

An entry's picture is a crop of a recorded frame. The embedding kept with it already says which camera and moment; this adds the face's box (fractions of the
picture), so that a privacy mask drawn over that place later also hides the picture. Entries enrolled before this have no box: their picture stays visible, since
where in the frame it was cannot be known, and the ticket records that.
"""
from __future__ import annotations

import sqlite3

NAME = "face_provenance"


def upgrade(db: sqlite3.Connection) -> None:
    if "box_json" not in {row[1] for row in db.execute("PRAGMA table_info(face_embeddings)")}:
        db.execute("ALTER TABLE face_embeddings ADD COLUMN box_json TEXT")
