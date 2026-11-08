"""Settings for keeping live video ready (#216; see app/live_ready.py)."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app import live_ready
from app.principal import Administrator
from app.store import connect

router = APIRouter()

CHOICES = [
    {"id": "all", "label": "Ready for every camera",
     "description": "The NVR keeps each camera's live stream open all the time, so opening a camera shows video at once. Each held stream is one more connection to the camera."},
    {"id": "recent", "label": "Keep cameras that were just watched",
     "description": "A camera's stream stays open for ten minutes after the last person watching it leaves, so coming back to it is quick."},
    {"id": "off", "label": "Only while someone is watching",
     "description": "The stream closes five seconds after the last viewer leaves. The first person to open a camera waits for it to start, usually 2 to 6 seconds."},
]


class LiveReadyRequest(BaseModel):
    mode: Literal["off", "recent", "all"]


def _view(request: Request, mode: str) -> dict[str, object]:
    keeper = getattr(request.app.state, "live_ready", None)
    names: dict[str, str] = {}
    with connect() as db:
        names = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM cameras")}
    held = [{**item, "name": names.get(str(item["camera_id"]), "")} for item in (keeper.status() if keeper else [])]
    return {"mode": mode, "choices": CHOICES, "held": held, "limit": live_ready.MAX_READY_STREAMS}


@router.get("/api/live-ready")
def get_live_ready(request: Request, _: Administrator) -> dict[str, object]:
    with connect() as db:
        mode = live_ready.get_mode(db)
    return _view(request, mode)


@router.put("/api/live-ready")
def put_live_ready(payload: LiveReadyRequest, request: Request, user: Administrator) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            before = live_ready.get_mode(db)
            live_ready.set_mode(db, payload.mode)
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                       (int(user["id"]), "live_ready_updated", "live_ready", "1", json.dumps({"from": before, "to": payload.mode}, separators=(",", ":"))))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    keeper = getattr(request.app.state, "live_ready", None)
    if keeper:
        keeper.nudge()
    return _view(request, payload.mode)
