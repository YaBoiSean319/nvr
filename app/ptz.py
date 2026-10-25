"""PTZ control: who may move a camera (the control lease), its presets, and patrol tours.

One person controls a camera at a time. Their first command takes the lease and each
command renews it; it lapses LEASE_SECONDS after the last one. Others see who holds it;
an administrator can take it over. Tours step through presets on their schedule and
wait while anyone holds the lease, then carry on with their next step.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .credentials import decrypt
from .onvif_ptz import Capabilities, PtzClient, PtzUnsupported
from .scheduling import mode_at
from .store import connect

LEASE_SECONDS = 60
CAPABILITY_CACHE_SECONDS = 300
TOUR_POLL_SECONDS = 1.0

_capabilities: dict[str, tuple[float, Capabilities | None, str | None]] = {}


class NotControllable(Exception):
    """The camera cannot be moved: no ONVIF device service or no PTZ."""


class LeaseHeld(Exception):
    def __init__(self, holder: str) -> None:
        super().__init__(f"{holder} is controlling this camera")
        self.holder = holder


def _now() -> datetime:
    return datetime.now(timezone.utc)


def forget_capabilities() -> None:
    from .onvif_ptz import _discovered

    _capabilities.clear()
    _discovered.clear()


def client_for(db: sqlite3.Connection, camera_id: str) -> PtzClient:
    camera = db.execute("SELECT device_service_url,motion_credentials_ciphertext,onvif_profile_token FROM cameras WHERE id=? AND archived_at IS NULL",
                        (camera_id,)).fetchone()
    if camera is None:
        raise LookupError("Camera not found")
    if not camera["device_service_url"]:
        raise NotControllable("This camera has no ONVIF device service.")
    credentials = json.loads(decrypt(camera["motion_credentials_ciphertext"])) if camera["motion_credentials_ciphertext"] else {}
    return PtzClient(camera["device_service_url"], credentials.get("username", ""), credentials.get("password", ""), camera["onvif_profile_token"])


async def capabilities(camera_id: str) -> tuple[Capabilities | None, str | None]:
    """What the camera can do, or why it cannot be moved. Remembered for a few minutes per device address."""
    try:
        with connect() as db:
            client = client_for(db, camera_id)
    except NotControllable as exc:
        return None, str(exc)
    key = f"{camera_id} {client.device_service_url}"
    cached = _capabilities.get(key)
    if cached and time.monotonic() - cached[0] < CAPABILITY_CACHE_SECONDS:
        return cached[1], cached[2]
    try:
        found, reason = await client.capabilities(), None
        if not (found.pan_tilt or found.zoom):
            found, reason = None, "This camera does not report pan, tilt or zoom."
    except PtzUnsupported:
        found, reason = None, "This camera has no PTZ service."
    except Exception:  # noqa: BLE001 - an unreachable camera is asked again after 15 seconds
        _capabilities[key] = (time.monotonic() - CAPABILITY_CACHE_SECONDS + 15, None, "The camera did not answer.")
        return None, "The camera did not answer."
    _capabilities[key] = (time.monotonic(), found, reason)
    return found, reason


def lease_view(db: sqlite3.Connection, camera_id: str, user_id: int) -> dict[str, Any] | None:
    row = db.execute("SELECT l.*,u.username FROM ptz_leases l JOIN users u ON u.id=l.user_id WHERE l.camera_id=? AND l.expires_at>?",
                     (camera_id, _now().isoformat())).fetchone()
    if row is None:
        return None
    return {"holder": row["username"], "mine": int(row["user_id"]) == user_id, "acquired_at": row["acquired_at"], "expires_at": row["expires_at"]}


def acquire(camera_id: str, user_id: int, *, take_over: bool = False) -> dict[str, Any]:
    """Take or renew the lease. Someone else's live lease refuses, unless an administrator takes over."""
    now = _now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            current = db.execute("SELECT l.user_id,l.acquired_at,u.username FROM ptz_leases l JOIN users u ON u.id=l.user_id WHERE l.camera_id=? AND l.expires_at>?",
                                 (camera_id, now.isoformat())).fetchone()
            if current is not None and int(current["user_id"]) != user_id and not take_over:
                raise LeaseHeld(current["username"])
            acquired = current["acquired_at"] if current is not None and int(current["user_id"]) == user_id else now.isoformat()
            db.execute("INSERT INTO ptz_leases(camera_id,user_id,acquired_at,expires_at) VALUES(?,?,?,?) "
                       "ON CONFLICT(camera_id) DO UPDATE SET user_id=excluded.user_id,acquired_at=excluded.acquired_at,expires_at=excluded.expires_at",
                       (camera_id, user_id, acquired, (now + timedelta(seconds=LEASE_SECONDS)).isoformat()))
            if take_over and current is not None and int(current["user_id"]) != user_id:
                db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                           (user_id, "ptz_lease_taken_over", "camera", camera_id, json.dumps({"from": current["username"]})))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return lease_view(db, camera_id, user_id) or {}


def release(camera_id: str, user_id: int) -> None:
    with connect() as db:
        db.execute("DELETE FROM ptz_leases WHERE camera_id=? AND user_id=?", (camera_id, user_id))


def lease_held(db: sqlite3.Connection, camera_id: str) -> bool:
    return db.execute("SELECT 1 FROM ptz_leases WHERE camera_id=? AND expires_at>?", (camera_id, _now().isoformat())).fetchone() is not None


def preset_view(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "token": row["token"]}


def presets(db: sqlite3.Connection, camera_id: str) -> list[dict[str, Any]]:
    return [preset_view(row) for row in db.execute("SELECT * FROM ptz_presets WHERE camera_id=? ORDER BY name COLLATE NOCASE", (camera_id,))]


def tour_view(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    state = db.execute("SELECT * FROM ptz_tour_state WHERE tour_id=?", (row["id"],)).fetchone()
    return {"id": row["id"], "name": row["name"], "enabled": bool(row["enabled"]), "steps": json.loads(row["steps_json"]),
            "schedule": json.loads(row["schedule_json"]) if row["schedule_json"] else None,
            "state": state["state"] if state else "idle", "step": state["step"] if state else 0}


def tours(db: sqlite3.Connection, camera_id: str) -> list[dict[str, Any]]:
    return [tour_view(db, row) for row in db.execute("SELECT * FROM ptz_tours WHERE camera_id=? ORDER BY name COLLATE NOCASE", (camera_id,))]


def capability_view(found: Capabilities | None) -> dict[str, Any] | None:
    if found is None:
        return None
    view = asdict(found)
    view.pop("spaces", None)
    return view


def _scheduled(schedule: dict[str, Any] | None, at: datetime) -> bool:
    if schedule is None:
        return True
    intervals = [{**interval, "mode": "continuous"} for interval in schedule["intervals"]]
    return mode_at(intervals, schedule["timezone"], at) != "disabled"


@dataclass(frozen=True)
class _TourMove:
    tour_id: str
    updated_at: str  # The tour as it was read: an edit meanwhile restarts it, so this move's result is dropped.
    camera_id: str
    token: str
    step: int
    next_step: int
    dwell: float


def _due_moves(now: datetime) -> list[_TourMove]:
    moves = []
    with connect() as db:
        for tour in db.execute("SELECT * FROM ptz_tours WHERE enabled=1").fetchall():
            state = db.execute("SELECT * FROM ptz_tour_state WHERE tour_id=?", (tour["id"],)).fetchone()
            steps = json.loads(tour["steps_json"])
            if not steps or not _scheduled(json.loads(tour["schedule_json"]) if tour["schedule_json"] else None, now):
                db.execute("INSERT INTO ptz_tour_state(tour_id,step,next_at,state) VALUES(?,0,NULL,'idle') "
                           "ON CONFLICT(tour_id) DO UPDATE SET next_at=NULL,state='idle'", (tour["id"],))
                continue
            if lease_held(db, tour["camera_id"]):
                # Someone is driving: wait, and move to the next step as soon as they let go.
                db.execute("INSERT INTO ptz_tour_state(tour_id,step,next_at,state) VALUES(?,0,NULL,'paused') "
                           "ON CONFLICT(tour_id) DO UPDATE SET next_at=NULL,state='paused'", (tour["id"],))
                continue
            if state is not None and state["next_at"] and datetime.fromisoformat(state["next_at"]) > now:
                continue
            start = (state["step"] if state is not None else 0) % len(steps)
            # A step whose preset has gone is skipped rather than holding the tour forever.
            for offset in range(len(steps)):
                step = (start + offset) % len(steps)
                preset = db.execute("SELECT token FROM ptz_presets WHERE id=? AND camera_id=?", (steps[step]["preset_id"], tour["camera_id"])).fetchone()
                if preset is not None:
                    moves.append(_TourMove(tour["id"], tour["updated_at"], tour["camera_id"], preset["token"], step,
                                           (step + 1) % len(steps), float(steps[step]["dwell_seconds"])))
                    break
    return moves


async def _run_move(move: _TourMove, now: datetime) -> bool:
    next_step, dwell, recalled = move.next_step, move.dwell, False
    try:
        with connect() as db:
            # Someone may have taken control since the tour was read: leave the camera to them.
            if lease_held(db, move.camera_id):
                return False
            client = client_for(db, move.camera_id)
        await client.goto_preset(move.token)
        recalled = True
    except Exception:  # noqa: BLE001 - the camera did not answer: try this step again shortly
        next_step, dwell = move.step, 5.0
    with connect() as db:
        # Only if the tour is unchanged: an edited tour starts from its first step, a deleted one has no state.
        if db.execute("SELECT 1 FROM ptz_tours WHERE id=? AND updated_at=? AND enabled=1", (move.tour_id, move.updated_at)).fetchone():
            db.execute("INSERT INTO ptz_tour_state(tour_id,step,next_at,state) VALUES(?,?,?,'running') "
                       "ON CONFLICT(tour_id) DO UPDATE SET step=excluded.step,next_at=excluded.next_at,state='running'",
                       (move.tour_id, next_step, (now + timedelta(seconds=dwell)).isoformat()))
    return recalled


async def tick_tours() -> int:
    """Advance every enabled tour that is due, cameras side by side. Returns how many presets were recalled."""
    now = _now()
    results = await asyncio.gather(*(_run_move(move, now) for move in await asyncio.to_thread(_due_moves, now)), return_exceptions=True)
    return sum(1 for result in results if result is True)


class PtzTourManager:
    def __init__(self, poll_seconds: float = TOUR_POLL_SECONDS) -> None:
        self.poll_seconds = poll_seconds
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="ptz-tours")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _run(self) -> None:
        while True:
            try:
                await tick_tours()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(self.poll_seconds)


def new_id() -> str:
    return str(uuid.uuid4())
