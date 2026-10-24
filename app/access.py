"""Who may see which camera.

Every signed-in user may see every camera until access grants (#119) arrive. Code
that hands camera data to a particular person outside a request (for example push
notifications) asks here, so grants apply to it the moment they exist.
"""
from __future__ import annotations

import sqlite3


def may_view_camera(db: sqlite3.Connection, user_id: int, camera_id: str) -> bool:
    user = db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
    camera = db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone()
    return user is not None and camera is not None
