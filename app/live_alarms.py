"""Alarm-driven Live layouts: each camera's neighbours and the feed of alarms that pop into Live.

An alarm pops into Live when the rule that raised it was set to, at the time it was
raised (``alarm_pop_ins`` records it with the alarm). Live shows the alarm's camera followed by its neighbours; which people see
it, and whether their Live follows it, is decided by the browser (each person opts in).
"""
from __future__ import annotations

import json
import sqlite3

from .access import may_view_camera
from .store import connect

MAX_NEIGHBOURS = 15  # With the alarm's own camera, a full 16-camera Live grid.
MAX_POP_INS = 20


class NeighbourError(ValueError):
    """The neighbour list names the camera itself, an unknown camera, or too many."""


def camera_exists(db: sqlite3.Connection, camera_id: str) -> bool:
    return db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is not None


def neighbours(db: sqlite3.Connection, camera_id: str, *, include_archived: bool = True) -> list[str]:
    archived = "" if include_archived else " AND c.archived_at IS NULL"
    return [row["neighbour_id"] for row in db.execute(
        "SELECT n.neighbour_id FROM camera_neighbours n JOIN cameras c ON c.id=n.neighbour_id "
        f"WHERE n.camera_id=?{archived} ORDER BY n.position", (camera_id,))]


def set_neighbours(camera_id: str, camera_ids: list[str], actor_id: int) -> list[str]:
    if len(camera_ids) > MAX_NEIGHBOURS:
        raise NeighbourError(f"Choose up to {MAX_NEIGHBOURS} neighbouring cameras")
    if len(set(camera_ids)) != len(camera_ids):
        raise NeighbourError("Choose each neighbouring camera once")
    if camera_id in camera_ids:
        raise NeighbourError("A camera cannot be its own neighbour")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not camera_exists(db, camera_id):
                raise LookupError("Camera not found")
            for neighbour in camera_ids:
                if not camera_exists(db, neighbour):
                    raise NeighbourError("Unknown camera in the neighbour list")
            db.execute("DELETE FROM camera_neighbours WHERE camera_id=?", (camera_id,))
            db.executemany("INSERT INTO camera_neighbours(camera_id,neighbour_id,position) VALUES(?,?,?)",
                           [(camera_id, neighbour, position) for position, neighbour in enumerate(camera_ids)])
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                       (actor_id, "camera_neighbours_updated", "camera", camera_id, json.dumps({"camera_ids": camera_ids}, separators=(",", ":"))))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return neighbours(db, camera_id)


def parse_cursor(cursor: str) -> int:
    if not cursor.isdigit():
        raise ValueError("Unknown pop-in cursor")
    return int(cursor)


def pop_ins(user_id: int, after: str | None) -> dict[str, object]:
    """Alarms that pop into Live raised after ``after``, oldest first.

    Without a cursor, nothing is returned: Live starts following from now, and
    alarms raised before it opened are never replayed.
    """
    with connect() as db:
        if after is None:
            latest = db.execute("SELECT COALESCE(MAX(sequence),0) FROM alarm_pop_ins").fetchone()[0]
            return {"cursor": str(latest), "alarms": []}
        rows = db.execute(
            "SELECT p.sequence, a.*, c.name AS camera_name FROM alarm_pop_ins p JOIN alarms a ON a.id=p.alarm_id JOIN cameras c ON c.id=a.camera_id "
            "WHERE p.sequence>? ORDER BY p.sequence LIMIT ?",
            (parse_cursor(after), MAX_POP_INS),
        ).fetchall()
        alarms = []
        for row in rows:
            if not may_view_camera(db, user_id, row["camera_id"]):
                continue
            visible = [camera for camera in neighbours(db, row["camera_id"], include_archived=False) if may_view_camera(db, user_id, camera)]
            alarms.append({
                "id": row["id"], "rule_name": row["rule_name"], "camera_id": row["camera_id"], "camera_name": row["camera_name"],
                "label": row["label"], "occurred_at": row["occurred_at"], "camera_ids": [row["camera_id"], *visible],
            })
        return {"cursor": str(rows[-1]["sequence"]) if rows else after, "alarms": alarms}
