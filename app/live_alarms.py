"""Alarm-driven Live layouts: each camera's neighbours and the feed of alarms that pop into Live.

An alarm pops into Live when the rule that raised it was set to, at the time it was
raised. Live shows the alarm's camera followed by its neighbours; which people see
it, and whether their Live follows it, is decided by the browser (each person opts in).
"""
from __future__ import annotations

import sqlite3

from .access import may_view_camera
from .store import connect

MAX_NEIGHBOURS = 15  # With the alarm's own camera, a full 16-camera Live grid.
MAX_POP_INS = 20


class NeighbourError(ValueError):
    """The neighbour list names the camera itself, an unknown camera, or too many."""


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
            if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
                raise LookupError("Camera not found")
            for neighbour in camera_ids:
                if db.execute("SELECT 1 FROM cameras WHERE id=?", (neighbour,)).fetchone() is None:
                    raise NeighbourError("Unknown camera in the neighbour list")
            db.execute("DELETE FROM camera_neighbours WHERE camera_id=?", (camera_id,))
            db.executemany("INSERT INTO camera_neighbours(camera_id,neighbour_id,position) VALUES(?,?,?)",
                           [(camera_id, neighbour, position) for position, neighbour in enumerate(camera_ids)])
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                       (actor_id, "camera_neighbours_updated", "camera", camera_id, f'{{"count":{len(camera_ids)}}}'))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return neighbours(db, camera_id)


def _cursor(row: sqlite3.Row | None) -> str:
    return f"{row['created_at']}|{row['id']}" if row else "|"


def parse_cursor(cursor: str) -> tuple[str, str]:
    created_at, separator, alarm_id = cursor.partition("|")
    if not separator:
        raise ValueError("Unknown pop-in cursor")
    return created_at, alarm_id


def pop_ins(user_id: int, after: str | None) -> dict[str, object]:
    """Alarms that pop into Live raised after ``after``, oldest first.

    Without a cursor, nothing is returned: Live starts following from now, and
    alarms raised before it opened are never replayed.
    """
    with connect() as db:
        latest = "SELECT id,created_at FROM alarms WHERE pop_into_live=1 ORDER BY created_at DESC, id DESC LIMIT 1"
        if after is None:
            return {"cursor": _cursor(db.execute(latest).fetchone()), "alarms": []}
        created_at, alarm_id = parse_cursor(after)
        rows = db.execute(
            "SELECT a.*, c.name AS camera_name FROM alarms a JOIN cameras c ON c.id=a.camera_id "
            "WHERE a.pop_into_live=1 AND (a.created_at>? OR (a.created_at=? AND a.id>?)) ORDER BY a.created_at, a.id LIMIT ?",
            (created_at, created_at, alarm_id, MAX_POP_INS),
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
        return {"cursor": _cursor(rows[-1]) if rows else after, "alarms": alarms}
