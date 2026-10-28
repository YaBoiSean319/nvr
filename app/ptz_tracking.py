"""PTZ auto-tracking (#145): the server steers a camera to keep a chosen person or vehicle centred and sized, from the AI worker's live detections.

Tracking runs only while it holds the camera's PTZ lease (#114) as a ``tracking`` lease: it takes the lease when it starts, renews it every pass, and gives it up
when it ends. Anyone's manual command takes the lease from it and ends the tracking at once; every pass, and every command, first checks that the lease is still
this tracking's own, so a camera is never steered without it. A target that is not seen for ``lost_seconds`` stops the camera; if it stays lost for
``home_timeout_seconds`` the camera goes to its home preset and the tracking ends. A target in a privacy-masked area counts as not seen, so the camera never follows
what a mask hides.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import privacy, ptz
from .audit import record
from .store import connect

logger = logging.getLogger(__name__)

POLL_SECONDS = 0.5
RECENT_SECONDS = 10.0  # A target to start on must have been seen this recently
DEAD_ZONE = 0.06  # Off-centre by less than this (a fraction of the picture) is centred enough
ZOOM_DEAD_ZONE = 0.1
PAN_TILT_GAIN = 1.6
ZOOM_GAIN = 1.2
MAX_SPEED = 0.6
ZOOM_MAX_SPEED = 0.4
MOVE_SECONDS = 0.8  # Each command moves the camera for this long and no longer: a stalled server leaves the camera still
ZOOM_ONLY_WHEN_WITHIN = 0.25  # Zoom only once the target is this close to the centre
FAILURES_BEFORE_GIVING_UP = 3
DEFAULT_SETTINGS = {"home_preset_id": None, "lost_seconds": 5.0, "home_timeout_seconds": 30, "target_size": 0.4, "zoom_enabled": True}


class TrackingRefused(Exception):
    """Tracking cannot start; the message is for the person who asked."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def settings_for(db: sqlite3.Connection, camera_id: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM ptz_tracking_settings WHERE camera_id=?", (camera_id,)).fetchone()
    if row is None:
        return dict(DEFAULT_SETTINGS)
    return {"home_preset_id": row["home_preset_id"], "lost_seconds": float(row["lost_seconds"]), "home_timeout_seconds": int(row["home_timeout_seconds"]),
            "target_size": float(row["target_size"]), "zoom_enabled": bool(row["zoom_enabled"])}


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def control(box: dict[str, float], *, target_size: float, can_zoom: bool) -> tuple[float, float, float]:
    """Pan, tilt and zoom speeds (-1..1) that bring the box to the centre of the picture and to ``target_size`` of its height.

    Positive pan is right, positive tilt is up, positive zoom is in. A target that is centred enough, and sized enough, gets no command."""
    error_x = box["x"] + box["width"] / 2 - 0.5
    error_y = box["y"] + box["height"] / 2 - 0.5
    pan = _clamp(PAN_TILT_GAIN * error_x, MAX_SPEED) if abs(error_x) > DEAD_ZONE else 0.0
    tilt = _clamp(-PAN_TILT_GAIN * error_y, MAX_SPEED) if abs(error_y) > DEAD_ZONE else 0.0
    zoom = 0.0
    if can_zoom and abs(error_x) < ZOOM_ONLY_WHEN_WITHIN and abs(error_y) < ZOOM_ONLY_WHEN_WITHIN:
        size = max(box["height"], box["width"] * 0.5625)  # A vehicle is wide: size it by its width on a 16:9 picture
        if abs(target_size - size) > ZOOM_DEAD_ZONE:
            zoom = _clamp(ZOOM_GAIN * (target_size - size), ZOOM_MAX_SPEED)
    return round(pan, 4), round(tilt, 4), round(zoom, 4)


def _touches(box: dict[str, float], zones: list[dict[str, float]]) -> bool:
    left, top, right, bottom = box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]
    return any(left < zone["x"] + zone["width"] and right > zone["x"] and top < zone["y"] + zone["height"] and bottom > zone["y"] for zone in zones)


def _latest(db: sqlite3.Connection, track_id: str, zones: list[dict[str, float]]) -> tuple[str, dict[str, float], str] | None:
    """The target's newest detection as (when, box, label), or None when there is none or it is in a masked area."""
    row = db.execute("SELECT detected_at,label,box_x,box_y,box_width,box_height FROM ai_detections WHERE track_id=? ORDER BY detected_at DESC,id DESC LIMIT 1", (track_id,)).fetchone()
    if row is None:
        return None
    box = {"x": row["box_x"], "y": row["box_y"], "width": row["box_width"], "height": row["box_height"]}
    if zones and _touches(box, zones):
        return None
    return row["detected_at"], box, row["label"]


def view(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"track_id": row["track_id"], "label": row["label"], "source": row["source"], "state": row["state"], "started_at": row["started_at"],
            "last_seen_at": row["last_seen_at"], "lost_since": row["lost_since"]}


def _holds_lease(db: sqlite3.Connection, camera_id: str, user_id: int, now: datetime) -> bool:
    return db.execute("SELECT 1 FROM ptz_leases WHERE camera_id=? AND user_id=? AND purpose='tracking' AND expires_at>?", (camera_id, user_id, now.isoformat())).fetchone() is not None


def _end(db: sqlite3.Connection, row: sqlite3.Row, reason: str, *, release_lease: bool) -> bool:
    """Forget this tracking (only if it is still the one in the table) and, when asked, let go of its lease. True when it was still there."""
    gone = db.execute("DELETE FROM ptz_tracking WHERE camera_id=? AND track_id=? AND started_at=?", (row["camera_id"], row["track_id"], row["started_at"])).rowcount
    if release_lease:
        db.execute("DELETE FROM ptz_leases WHERE camera_id=? AND user_id=? AND purpose='tracking'", (row["camera_id"], row["lease_user_id"]))
    if gone:
        record(db, row["started_by"], "ptz_tracking_ended", "camera", row["camera_id"], track_id=row["track_id"], reason=reason, source=row["source"])
    return bool(gone)


def end(camera_id: str, reason: str) -> None:
    with connect() as db:
        row = db.execute("SELECT * FROM ptz_tracking WHERE camera_id=?", (camera_id,)).fetchone()
        if row is not None:
            _end(db, row, reason, release_lease=True)


async def start(camera_id: str, track_id: str, user_id: int, *, source: str, started_by: int | None = None, rule_id: str | None = None) -> dict[str, Any]:
    """Start following a track with a camera. ``user_id`` holds the lease while it does (the operator who chose the target, or the rule's author).

    An operator's choice takes the camera from any tracking and from nobody else; a rule's takes it only when nothing is tracking and no person is driving."""
    found, reason = await ptz.capabilities(camera_id)
    if found is None or not found.pan_tilt:
        raise TrackingRefused(reason or "This camera cannot pan or tilt.", "not_controllable")
    now = _now()
    with connect() as db:
        track = db.execute("SELECT id,camera_id,label,last_seen_at FROM ai_tracks WHERE id=?", (track_id,)).fetchone()
        if track is None or track["camera_id"] != camera_id:
            raise TrackingRefused("That object is not on this camera.", "track_gone")
        latest = _latest(db, track_id, privacy.server_zones(db, camera_id))
        if latest is None or (now - _parse(latest[0])).total_seconds() > RECENT_SECONDS:
            raise TrackingRefused("That object is no longer in view.", "track_gone")
        if source == "rule":
            if db.execute("SELECT 1 FROM ptz_tracking WHERE camera_id=?", (camera_id,)).fetchone():
                raise TrackingRefused("The camera is already following something.", "already_tracking")
            if ptz.lease_held(db, camera_id):
                raise TrackingRefused("Someone is controlling this camera.", "lease_held")
    try:
        if source == "operator":
            ptz.acquire(camera_id, user_id)  # Manual first: it ends any tracking and refuses if somebody else is driving ...
        ptz.acquire(camera_id, user_id, purpose="tracking")  # ... then the lease becomes the tracking's own
    except ptz.LeaseHeld as exc:
        raise TrackingRefused(f"{exc.holder} is controlling this camera.", "lease_held") from exc
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO ptz_tracking(camera_id,track_id,label,source,lease_user_id,started_by,rule_id,started_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(camera_id) DO UPDATE SET track_id=excluded.track_id,label=excluded.label,source=excluded.source,lease_user_id=excluded.lease_user_id,"
                       "started_by=excluded.started_by,rule_id=excluded.rule_id,started_at=excluded.started_at,last_detection_at=NULL,last_seen_at=excluded.last_seen_at,"
                       "state='tracking',lost_since=NULL",
                       (camera_id, track_id, latest[2], source, user_id, started_by if started_by is not None else user_id, rule_id, now.isoformat(), latest[0]))
            record(db, started_by if started_by is not None else user_id, "ptz_tracking_started", "camera", camera_id, track_id=track_id, source=source, rule_id=rule_id)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return view(db.execute("SELECT * FROM ptz_tracking WHERE camera_id=?", (camera_id,)).fetchone()) or {}


def lease_user_for_rule(db: sqlite3.Connection, rule_id: str | None) -> int | None:
    """Who holds the lease for a rule's tracking: the rule's author, or an administrator when the author is gone."""
    if rule_id is not None:
        row = db.execute("SELECT u.id FROM rules r JOIN users u ON u.id=r.created_by WHERE r.id=?", (rule_id,)).fetchone()
        if row is not None:
            return int(row["id"])
    row = db.execute("SELECT id FROM users WHERE role='administrator' ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row is not None else None


@dataclass
class _Plan:
    kind: str  # abandon | wait | move | stop | home
    velocity: tuple[float, float, float] = (0.0, 0.0, 0.0)
    token: str | None = None
    reason: str = ""


_failures: dict[str, int] = {}


def _plan(row: sqlite3.Row, now: datetime, can_zoom: bool) -> _Plan:
    """Decide this pass for one tracking, and record what was seen. Runs in a thread."""
    camera_id = row["camera_id"]
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            current = db.execute("SELECT * FROM ptz_tracking WHERE camera_id=? AND track_id=? AND started_at=?", (camera_id, row["track_id"], row["started_at"])).fetchone()
            if current is None:
                db.execute("COMMIT")
                return _Plan("abandon", reason="replaced")  # Taken over by manual control, or by a newer tracking
            renewed = db.execute("UPDATE ptz_leases SET expires_at=? WHERE camera_id=? AND user_id=? AND purpose='tracking' AND expires_at>?",
                                 ((now + timedelta(seconds=ptz.LEASE_SECONDS)).isoformat(), camera_id, current["lease_user_id"], now.isoformat())).rowcount
            if not renewed:
                _end(db, current, "lease_lost", release_lease=False)
                db.execute("COMMIT")
                return _Plan("abandon", reason="lease_lost")
            settings = settings_for(db, camera_id)
            latest = _latest(db, current["track_id"], privacy.server_zones(db, camera_id))
            seen = latest is not None and (now - _parse(latest[0])).total_seconds() <= settings["lost_seconds"]
            if seen and latest is not None:
                if current["state"] == "lost":
                    db.execute("UPDATE ptz_tracking SET state='tracking',lost_since=NULL WHERE camera_id=?", (camera_id,))
                if latest[0] == current["last_detection_at"]:
                    db.execute("COMMIT")
                    return _Plan("wait")  # Nothing new to steer by
                db.execute("UPDATE ptz_tracking SET last_detection_at=?,last_seen_at=? WHERE camera_id=?", (latest[0], latest[0], camera_id))
                velocity = control(latest[1], target_size=settings["target_size"], can_zoom=can_zoom and settings["zoom_enabled"])
                db.execute("COMMIT")
                return _Plan("move", velocity) if any(velocity) else _Plan("wait")
            if current["state"] == "tracking":
                db.execute("UPDATE ptz_tracking SET state='lost',lost_since=? WHERE camera_id=?", (now.isoformat(), camera_id))
                db.execute("COMMIT")
                return _Plan("stop")
            lost_for = (now - _parse(current["lost_since"] or now.isoformat())).total_seconds()
            if lost_for >= settings["home_timeout_seconds"]:
                token = None
                if settings["home_preset_id"]:
                    preset = db.execute("SELECT token FROM ptz_presets WHERE id=? AND camera_id=?", (settings["home_preset_id"], camera_id)).fetchone()
                    token = preset["token"] if preset is not None else None
                db.execute("COMMIT")
                return _Plan("home", token=token)
            db.execute("COMMIT")
            return _Plan("wait")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise


def _still_ours(row: sqlite3.Row) -> bool:
    """The last check before a command goes to the camera: tracking is still in the table and still holds its own live lease."""
    with connect() as db:
        if db.execute("SELECT 1 FROM ptz_tracking WHERE camera_id=? AND track_id=? AND started_at=?", (row["camera_id"], row["track_id"], row["started_at"])).fetchone() is None:
            return False
        return _holds_lease(db, row["camera_id"], int(row["lease_user_id"]), _now())


async def _step(row: sqlite3.Row, now: datetime) -> str:
    camera_id = row["camera_id"]
    found, _ = await ptz.capabilities(camera_id)
    plan = await asyncio.to_thread(_plan, row, now, bool(found and found.zoom))
    if plan.kind in {"wait", "abandon"}:
        return plan.kind
    if not await asyncio.to_thread(_still_ours, row):
        return "abandon"
    try:
        with connect() as db:
            client = ptz.client_for(db, camera_id)
        if plan.kind == "move":
            await client.continuous_move(*plan.velocity, MOVE_SECONDS)
        elif plan.kind == "stop":
            await client.stop()
        elif plan.kind == "home":
            if plan.token:
                await client.goto_preset(plan.token)
        _failures.pop(camera_id, None)
    except Exception:  # noqa: BLE001 - the camera did not answer; after a few passes the tracking gives up
        count = _failures[camera_id] = _failures.get(camera_id, 0) + 1
        if count >= FAILURES_BEFORE_GIVING_UP:
            _failures.pop(camera_id, None)
            await asyncio.to_thread(end, camera_id, "camera_unreachable")
            return "failed"
        if plan.kind != "home":
            return "failed"
    if plan.kind == "home":
        await asyncio.to_thread(end, camera_id, "lost_returned_home" if plan.token else "lost")
        return "home"
    return plan.kind


async def tick(now: datetime | None = None) -> dict[str, int]:
    """One pass over every tracking: steer, stop or send home. Returns how many of each."""
    now = now or _now()
    with connect() as db:
        rows = db.execute("SELECT * FROM ptz_tracking").fetchall()
    results = await asyncio.gather(*(_step(row, now) for row in rows), return_exceptions=True)
    counts: dict[str, int] = {}
    for result in results:
        key = result if isinstance(result, str) else "error"
        counts[key] = counts.get(key, 0) + 1
    return counts


class PtzTrackingManager:
    def __init__(self, poll_seconds: float = POLL_SECONDS) -> None:
        self.poll_seconds = poll_seconds
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="ptz-tracking")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _run(self) -> None:
        while True:
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("PTZ tracking pass failed")
            await asyncio.sleep(self.poll_seconds)
