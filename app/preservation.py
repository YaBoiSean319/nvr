from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from .retention import unlink_if_unleased
from .storage import recording_file_path
from .store import connect


def active_hold(db, segment_id: int, now: datetime | None = None):
    now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    return db.execute(
        "SELECT * FROM recording_holds WHERE segment_id=? AND released_at IS NULL AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at DESC LIMIT 1",
        (segment_id,now),
    ).fetchone()


def preserve_segment(camera_id: str, segment_id: int, actor_id: int, expires_in_days: int | None = None) -> dict[str, object]:
    hold_id=str(uuid.uuid4())
    expires=(datetime.now(timezone.utc)+timedelta(days=expires_in_days)).isoformat() if expires_in_days else None
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        segment=db.execute("SELECT id,relative_path FROM recording_segments WHERE id=? AND camera_id=?",(segment_id,camera_id)).fetchone()
        if segment is None: raise LookupError("Recorded interval not found")
        existing=active_hold(db,segment_id)
        if existing: raise FileExistsError("Recorded interval is already preserved")
        path=recording_file_path(segment["relative_path"])
        if not path.is_file(): raise FileNotFoundError("Recorded interval is unavailable")
        db.execute("INSERT INTO recording_holds(id,segment_id,camera_id,created_by,expires_at) VALUES(?,?,?,?,?)",(hold_id,segment_id,camera_id,actor_id,expires))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (actor_id,"preserve","recording_segment",str(segment_id),json.dumps({"camera_id":camera_id,"expires_at":expires},separators=(",",":"))))
    return {"id":hold_id,"segment_id":segment_id,"camera_id":camera_id,"created_by":actor_id,"expires_at":expires,"released_at":None}


def release_segment_hold(camera_id: str, segment_id: int, actor_id: int) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        hold=active_hold(db,segment_id)
        if hold is None or hold["camera_id"]!=camera_id: raise LookupError("No active preservation hold found")
        db.execute("UPDATE recording_holds SET released_by=?,released_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",(actor_id,hold["id"]))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (actor_id,"release_hold","recording_segment",str(segment_id),json.dumps({"camera_id":camera_id,"hold_id":hold["id"]},separators=(",",":"))))


def delete_segment(camera_id: str, segment_id: int, actor_id: int) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        segment=db.execute("SELECT id,relative_path FROM recording_segments WHERE id=? AND camera_id=?",(segment_id,camera_id)).fetchone()
        if segment is None: raise LookupError("Recorded interval not found")
        if active_hold(db,segment_id): raise PermissionError("Preserved footage cannot be deleted")
        path=recording_file_path(segment["relative_path"])
        if not unlink_if_unleased(path): raise BlockingIOError("Recorded interval is currently being played")
        db.execute("DELETE FROM recording_segments WHERE id=?",(segment_id,))
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (actor_id,"delete","recording_segment",str(segment_id),json.dumps({"camera_id":camera_id},separators=(",",":"))))


def get_segment_hold(camera_id: str, segment_id: int) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM recording_segments WHERE id=? AND camera_id=?",(segment_id,camera_id)).fetchone() is None:
            raise LookupError("Recorded interval not found")
        hold=active_hold(db,segment_id)
    if hold is None: return {"camera_id":camera_id,"segment_id":segment_id,"held":False,"hold":None}
    return {"camera_id":camera_id,"segment_id":segment_id,"held":True,"hold":{key:hold[key] for key in ("id","created_by","created_at","expires_at")}}
