"""Rules over detections and tracks.

A rule watches cameras (listed directly or through their group) for objects of
chosen classes meeting one condition:

- ``zone_entry``: the object's centre is inside the zone (appearing inside counts);
- ``line_crossing``: the centre crosses the line segment, in the chosen direction.
  ``forward`` is the direction of the arrow the interface draws: rightwards for a
  line drawn top to bottom, upwards for a line drawn left to right;
- ``loitering``: the centre stays inside the zone for the dwell time;
- ``wrong_way``: inside the zone, the centre travels at least ``WRONG_WAY_DISTANCE``
  of the frame against the expected direction.

A condition is evaluated over a whole track, so each object produces at most one
rule event per rule. The earliest moment that meets the condition, falls inside
the rule's schedule and is outside its cooldown becomes the event. A saved rule
never fires for moments before it was last saved. Test mode replays the same
evaluation over stored tracks without writing anything.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .scheduling import mode_at
from .store import connect

logger = logging.getLogger("nvr.rules")

CONDITION_TYPES = ("zone_entry", "line_crossing", "loitering", "wrong_way")
WRONG_WAY_DISTANCE = 0.1
DIRECTIONS = {"right": (1.0, 0.0), "left": (-1.0, 0.0), "down": (0.0, 1.0), "up": (0.0, -1.0)}
BATCH_SIZE = 5000


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    enabled: bool
    camera_ids: frozenset[str]
    group_ids: frozenset[str]
    condition_type: str
    condition: dict[str, Any]
    classes: frozenset[str]
    schedule: dict[str, Any] | None
    cooldown: timedelta
    active_from: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Rule":
        return cls(
            id=row["id"], name=row["name"], enabled=bool(row["enabled"]),
            camera_ids=frozenset(json.loads(row["camera_ids_json"])), group_ids=frozenset(json.loads(row["group_ids_json"])),
            condition_type=row["condition_type"], condition=json.loads(row["condition_json"]),
            classes=frozenset(json.loads(row["classes_json"])),
            schedule=json.loads(row["schedule_json"]) if row["schedule_json"] else None,
            cooldown=timedelta(seconds=int(row["cooldown_seconds"])), active_from=_parse(row["active_from"]),
        )

    def watches(self, camera_id: str, group_id: str | None, label: str) -> bool:
        in_scope = camera_id in self.camera_ids or (group_id is not None and group_id in self.group_ids)
        return in_scope and label in self.classes

    def scheduled(self, at: datetime) -> bool:
        if self.schedule is None:
            return True
        intervals = [{**interval, "mode": "continuous"} for interval in self.schedule["intervals"]]
        return mode_at(intervals, self.schedule["timezone"], at) != "disabled"


@dataclass(frozen=True)
class PathPoint:
    detection_id: int
    at: datetime
    x: float
    y: float
    box: dict[str, float]


def path_from_rows(rows: Iterable[sqlite3.Row]) -> list[PathPoint]:
    return [PathPoint(row["id"], _parse(row["detected_at"]), row["box_x"] + row["box_width"] / 2, row["box_y"] + row["box_height"] / 2,
                      {"x": row["box_x"], "y": row["box_y"], "width": row["box_width"], "height": row["box_height"]}) for row in rows]


def _inside(zone: dict[str, float], point: PathPoint) -> bool:
    return zone["x"] <= point.x <= zone["x"] + zone["width"] and zone["y"] <= point.y <= zone["y"] + zone["height"]


def _zone_entries(condition: dict[str, Any], path: list[PathPoint]) -> list[PathPoint]:
    zone = condition["zone"]
    moments, was_inside = [], False
    for point in path:
        inside = _inside(zone, point)
        if inside and not was_inside:
            moments.append(point)
        was_inside = inside
    return moments


def _line_crossings(condition: dict[str, Any], path: list[PathPoint]) -> list[PathPoint]:
    line, direction = condition["line"], condition["direction"]
    ax, ay, bx, by = line["x1"], line["y1"], line["x2"], line["y2"]
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy
    # The forward normal (screen coordinates, y grows downwards).
    nx, ny = dy, -dx
    moments = []
    for before, after in zip(path, path[1:]):
        side_before = (before.x - ax) * nx + (before.y - ay) * ny
        side_after = (after.x - ax) * nx + (after.y - ay) * ny
        forward = side_before < 0 <= side_after
        reverse = side_before >= 0 > side_after
        if not (forward or reverse) or (direction == "forward" and not forward) or (direction == "reverse" and not reverse):
            continue
        fraction = side_before / (side_before - side_after)
        cross_x = before.x + (after.x - before.x) * fraction
        cross_y = before.y + (after.y - before.y) * fraction
        along = ((cross_x - ax) * dx + (cross_y - ay) * dy) / length_squared
        if 0 <= along <= 1:
            moments.append(after)
    return moments


def _loitering(condition: dict[str, Any], path: list[PathPoint]) -> list[PathPoint]:
    zone, dwell = condition["zone"], timedelta(seconds=condition["dwell_seconds"])
    moments: list[PathPoint] = []
    entered: datetime | None = None
    reported = False
    for point in path:
        if not _inside(zone, point):
            entered, reported = None, False
            continue
        entered = entered or point.at
        if not reported and point.at - entered >= dwell:
            moments.append(point)
            reported = True
    return moments


def _wrong_way(condition: dict[str, Any], path: list[PathPoint]) -> list[PathPoint]:
    zone = condition["zone"]
    ex, ey = DIRECTIONS[condition["direction"]]
    moments: list[PathPoint] = []
    anchor: PathPoint | None = None
    reported = False
    for point in path:
        if not _inside(zone, point):
            anchor, reported = None, False
            continue
        anchor = anchor or point
        if not reported and (point.x - anchor.x) * ex + (point.y - anchor.y) * ey <= -WRONG_WAY_DISTANCE:
            moments.append(point)
            reported = True
    return moments


_CONDITIONS = {"zone_entry": _zone_entries, "line_crossing": _line_crossings, "loitering": _loitering, "wrong_way": _wrong_way}


def condition_moments(rule: Rule, path: list[PathPoint]) -> list[PathPoint]:
    """Every moment along a track's path that meets the rule's condition, in order."""
    return _CONDITIONS[rule.condition_type](rule.condition, path)


def choose_moment(rule: Rule, path: list[PathPoint], last_event_before: Any, *, replay: bool = False) -> PathPoint | None:
    """The moment that becomes the track's event, or None.

    ``last_event_before(at)`` returns the rule's latest event on this camera at or
    before ``at``, so the cooldown can be applied.
    """
    for point in condition_moments(rule, path):
        if not replay and point.at < rule.active_from:
            continue
        if not rule.scheduled(point.at):
            continue
        previous = last_event_before(point.at) if rule.cooldown else None
        if previous is not None and point.at - previous < rule.cooldown:
            continue
        return point
    return None


def load_rules(db: sqlite3.Connection) -> list[Rule]:
    return [Rule.from_row(row) for row in db.execute("SELECT * FROM rules WHERE enabled=1 ORDER BY created_at,id")]


def _track_path(db: sqlite3.Connection, track_id: str) -> list[PathPoint]:
    return path_from_rows(db.execute(
        "SELECT id,detected_at,box_x,box_y,box_width,box_height FROM ai_detections WHERE track_id=? ORDER BY detected_at,id", (track_id,)))


def process_pending(batch_size: int = BATCH_SIZE) -> int:
    """Evaluate rules over detections stored since the last pass; return how many were evaluated."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            cursor = db.execute("SELECT last_detection_id FROM rules_cursor WHERE singleton_id=1").fetchone()[0]
            fresh = db.execute("SELECT id,track_id FROM ai_detections WHERE id>? ORDER BY id LIMIT ?", (cursor, batch_size)).fetchall()
            if not fresh:
                db.execute("COMMIT")
                return 0
            rules = load_rules(db)
            touched = list(dict.fromkeys(row["track_id"] for row in fresh))
            for track_id in touched if rules else ():
                track = db.execute(
                    "SELECT t.id,t.camera_id,t.label,c.group_id FROM ai_tracks t JOIN cameras c ON c.id=t.camera_id WHERE t.id=?", (track_id,)
                ).fetchone()
                if track is None:
                    continue
                watching = [rule for rule in rules if rule.watches(track["camera_id"], track["group_id"], track["label"])
                            and db.execute("SELECT 1 FROM rule_events WHERE rule_id=? AND track_id=?", (rule.id, track_id)).fetchone() is None]
                if not watching:
                    continue
                path = _track_path(db, track_id)
                for rule in watching:
                    def last_event_before(at: datetime, rule: Rule = rule) -> datetime | None:
                        row = db.execute("SELECT max(occurred_at) FROM rule_events WHERE rule_id=? AND camera_id=? AND occurred_at<=?",
                                         (rule.id, track["camera_id"], at.isoformat())).fetchone()
                        return _parse(row[0]) if row[0] else None

                    moment = choose_moment(rule, path, last_event_before)
                    if moment is not None:
                        db.execute(
                            "INSERT OR IGNORE INTO rule_events(id,rule_id,rule_name,camera_id,track_id,detection_id,label,condition_type,occurred_at,box_json) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (str(uuid.uuid4()), rule.id, rule.name, track["camera_id"], track_id, moment.detection_id, track["label"],
                             rule.condition_type, moment.at.isoformat(), json.dumps(moment.box, separators=(",", ":"))),
                        )
            db.execute("UPDATE rules_cursor SET last_detection_id=? WHERE singleton_id=1", (fresh[-1]["id"],))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return len(fresh)


def replay(rule: Rule, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """What the rule would have fired over stored tracks in [start, end); writes nothing."""
    start_text, end_text = start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
    with connect() as db:
        tracks = db.execute(
            "SELECT t.id,t.camera_id,t.label,c.name AS camera_name,c.group_id FROM ai_tracks t JOIN cameras c ON c.id=t.camera_id "
            "WHERE t.first_seen_at<? AND t.last_seen_at>=? ORDER BY t.first_seen_at,t.id", (end_text, start_text)).fetchall()
        candidates = []
        for track in tracks:
            if not rule.watches(track["camera_id"], track["group_id"], track["label"]):
                continue
            path = [point for point in _track_path(db, track["id"]) if start <= point.at < end]
            moments = condition_moments(rule, path)
            if moments:
                candidates.append((moments[0].at, track, path))
    fired: dict[str, list[datetime]] = {}
    events = []
    for _, track, path in sorted(candidates, key=lambda item: (item[0], item[1]["id"])):
        times = fired.setdefault(track["camera_id"], [])
        moment = choose_moment(rule, path, lambda at: max((time for time in times if time <= at), default=None), replay=True)
        if moment is None:
            continue
        times.append(moment.at)
        events.append({"camera_id": track["camera_id"], "camera_name": track["camera_name"], "track_id": track["id"], "label": track["label"],
                       "occurred_at": moment.at.isoformat(), "box": moment.box})
    return sorted(events, key=lambda event: event["occurred_at"])


class RulesManager:
    """Runs ``process_pending`` continuously; a nudge runs it now."""

    def __init__(self, poll_seconds: float = 1.0) -> None:
        self.poll_seconds = poll_seconds
        self.task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    async def start(self) -> None:
        self._wake = asyncio.Event()
        self.task = asyncio.create_task(self._run(), name="rules")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        # Evaluate what was stored before stopping, so nothing waits for the next start.
        await asyncio.to_thread(self._drain)

    def nudge(self) -> None:
        self._wake.set()

    def _drain(self) -> None:
        while process_pending():
            pass

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._drain)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep evaluating; the next pass retries the batch
                logger.exception("Rule evaluation failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

