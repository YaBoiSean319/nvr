"""Privacy masks: each camera's masked areas, whether the camera itself applies them,
and the short grants that let an administrator see unmasked footage for a stated reason."""
from __future__ import annotations

import sqlite3

NAME = "privacy_masks"

_STATEMENTS = (
    # camera_masked_since: when the camera accepted the masks. Footage recorded from then on
    # carries them; anything older, and everything while it is NULL, is masked by the NVR.
    """
    CREATE TABLE privacy_masks (
        camera_id TEXT PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
        zones_json TEXT NOT NULL,
        camera_side TEXT NOT NULL DEFAULT 'none' CHECK (camera_side IN ('none','applied','unsupported','failed')),
        camera_mask_tokens_json TEXT NOT NULL DEFAULT '[]',
        camera_masked_since TEXT,
        -- The device the masks were written to: a camera replaced at another address is not trusted.
        written_to TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE unmasked_grants (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX unmasked_grants_lookup ON unmasked_grants(user_id, camera_id, expires_at)",
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
