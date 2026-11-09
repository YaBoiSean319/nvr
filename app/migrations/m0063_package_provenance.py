"""A case package records which cameras and privacy masks it was made with (#302).

A package is a copy of each clip's export. Whether it may still be handed out depends on today's camera access and on whether a mask has been drawn over a camera since its
footage was rendered. The exports it was made from expire and the case's clips can be removed, so what the check needs is kept with the package itself: the cameras in it,
when each was rendered, and the mask each camera had. Packages built before this have none and are worked out from their clips when asked for.
"""
from __future__ import annotations

import sqlite3

NAME = "package_provenance"


def upgrade(db: sqlite3.Connection) -> None:
    if "provenance_json" not in {row[1] for row in db.execute("PRAGMA table_info(case_packages)")}:
        db.execute("ALTER TABLE case_packages ADD COLUMN provenance_json TEXT")
