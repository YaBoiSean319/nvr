"""Rules over detections and tracks.

A rule watches cameras (listed directly, or as members of a chosen group) for
objects of chosen classes meeting one condition:

- ``zone_entry``: the object's centre is inside the zone (appearing inside counts);
- ``line_crossing``: the centre crosses the line segment, in the chosen direction.
  ``forward`` is the direction of the arrow the interface draws: rightwards for a
  line drawn top to bottom, upwards for a line drawn left to right;
- ``loitering``: the centre stays inside the zone for the dwell time;
- ``wrong_way``: inside the zone, the centre travels at least ``WRONG_WAY_DISTANCE``
  of the frame against the expected direction from the furthest point it reached.

A condition is evaluated over a whole track, so each object produces at most one
rule event per rule. The earliest moment that meets the condition, falls inside
the rule's schedule and is not within the cooldown of another event on the same
camera becomes the event. A saved rule never fires for moments before it was last
saved. Every rule event raises an alarm (see ``app.alarms``). Test mode replays
the same evaluation over stored tracks without writing.

An ``external_event`` rule is not about objects either: it fires when an event from a building system (a door forced, a panic button; see
``app.external_events``) is tied to a camera it watches, filtered by source, event type and subject, with the usual schedule and cooldown. Test mode
replays the events kept.

A ``tamper`` rule is not about objects. It fires when camera tamper detection (see ``app.tamper``) confirms a covered,
moved or defocused camera, once per detection and for the kinds the rule lists (all three if it lists none): its schedule,
cooldown and scope apply as to any rule, and it raises an alarm labelled with the kind. Test mode replays the tamper
incidents of the last day.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from .alarms import create_alarm
from .scheduling import mode_at
from .store import connect

logger = logging.getLogger("nvr.rules")

WRONG_WAY_DISTANCE = 0.1
TAMPER_KINDS = ("covered", "moved", "defocused")
FULL_FRAME = '{"x":0,"y":0,"width":1,"height":1}'
DIRECTIONS = {"right": (1.0, 0.0), "left": (-1.0, 0.0), "down": (0.0, 1.0), "up": (0.0, -1.0)}
BATCH_SIZE = 5000

# A camera's groups: its policy group and every group it browses in.
CAMERA_GROUPS_SQL = ("SELECT group_id FROM cameras WHERE id=? AND group_id IS NOT NULL "
                     "UNION SELECT group_id FROM camera_group_memberships WHERE camera_id=?")


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

    def watches(self, camera_id: str, groups: Iterable[str], label: str) -> bool:
        in_scope = camera_id in self.camera_ids or not self.group_ids.isdisjoint(groups)
        return in_scope and label in self.classes

    def watches_camera(self, camera_id: str, groups: Iterable[str]) -> bool:
        return camera_id in self.camera_ids or not self.group_ids.isdisjoint(groups)

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
    furthest: float | None = None
    reported = False
    for point in path:
        if not _inside(zone, point):
            furthest, reported = None, False
            continue
        progress = point.x * ex + point.y * ey
        furthest = progress if furthest is None else max(furthest, progress)
        if not reported and furthest - progress >= WRONG_WAY_DISTANCE:
            moments.append(point)
            reported = True
    return moments


_CONDITIONS = {"zone_entry": _zone_entries, "line_crossing": _line_crossings, "loitering": _loitering, "wrong_way": _wrong_way}


def condition_moments(rule: Rule, path: list[PathPoint]) -> list[PathPoint]:
    """Every moment along a track's path that meets the rule's condition, in order."""
    return _CONDITIONS[rule.condition_type](rule.condition, path)


def eligible_moments(rule: Rule, path: list[PathPoint], *, since: datetime | None, until: datetime | None = None) -> list[PathPoint]:
    """Moments that meet the condition inside the schedule and [since, until); the cooldown is applied after."""
    return [point for point in condition_moments(rule, path)
            if (since is None or point.at >= since) and (until is None or point.at < until) and rule.scheduled(point.at)]


def choose_moment(rule: Rule, moments: list[PathPoint], cooled: Callable[[datetime], bool]) -> PathPoint | None:
    """The first moment no other event on the camera is within the cooldown of."""
    return next((point for point in moments if not (rule.cooldown and cooled(point.at))), None)


def load_rules(db: sqlite3.Connection) -> list[Rule]:
    rules = []
    for row in db.execute("SELECT * FROM rules WHERE enabled=1 ORDER BY created_at,id"):
        try:
            rules.append(Rule.from_row(row))
        except (ValueError, KeyError, TypeError):
            logger.exception("Rule %s cannot be read; it is skipped", row["id"])
    return rules


def _track_path(db: sqlite3.Connection, track_id: str) -> list[PathPoint]:
    return path_from_rows(db.execute(
        "SELECT id,detected_at,box_x,box_y,box_width,box_height FROM ai_detections WHERE track_id=? ORDER BY detected_at,id", (track_id,)))


def _camera_groups(db: sqlite3.Connection, camera_id: str) -> set[str]:
    return {row[0] for row in db.execute(CAMERA_GROUPS_SQL, (camera_id, camera_id))}


def process_pending(batch_size: int = BATCH_SIZE) -> int:
    """Evaluate rules over detections stored since the last pass; return how many were evaluated.

    Evaluation reads without holding the write lock, so detection writes are never
    held up; only recording the resulting events and advancing the cursor takes it.
    """
    with connect() as db:
        cursor = db.execute("SELECT last_detection_id FROM rules_cursor WHERE singleton_id=1").fetchone()[0]
        fresh = db.execute("SELECT id,track_id FROM ai_detections WHERE id>? ORDER BY id LIMIT ?", (cursor, batch_size)).fetchall()
        if not fresh:
            return 0
        rules = load_rules(db)
        pending: list[tuple[Rule, sqlite3.Row, list[PathPoint]]] = []
        for track_id in dict.fromkeys(row["track_id"] for row in fresh) if rules else ():
            track = db.execute("SELECT id,camera_id,label FROM ai_tracks WHERE id=?", (track_id,)).fetchone()
            if track is None:
                continue
            groups = _camera_groups(db, track["camera_id"])
            watching = [rule for rule in rules if rule.watches(track["camera_id"], groups, track["label"])
                        and db.execute("SELECT 1 FROM rule_events WHERE rule_id=? AND track_id=?", (rule.id, track_id)).fetchone() is None]
            if not watching:
                continue
            path = _track_path(db, track_id)
            for rule in watching:
                try:
                    moments = eligible_moments(rule, path, since=rule.active_from)
                except Exception:  # noqa: BLE001 - one unreadable rule must not stop the others
                    logger.exception("Rule %s could not be evaluated; it is skipped", rule.id)
                    continue
                if moments:
                    pending.append((rule, track, moments))

        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT last_detection_id FROM rules_cursor WHERE singleton_id=1").fetchone()[0] != cursor:
                db.execute("ROLLBACK")
                return 0  # another pass already took this batch
            for rule, track, moments in pending:
                def cooled(at: datetime, rule: Rule = rule, camera_id: str = track["camera_id"]) -> bool:
                    return db.execute(
                        "SELECT 1 FROM rule_events WHERE rule_id=? AND camera_id=? AND occurred_at>? AND occurred_at<? LIMIT 1",
                        (rule.id, camera_id, (at - rule.cooldown).isoformat(), (at + rule.cooldown).isoformat())).fetchone() is not None

                moment = choose_moment(rule, moments, cooled)
                if moment is not None:
                    event_id = str(uuid.uuid4())
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO rule_events(id,rule_id,rule_name,camera_id,track_id,detection_id,label,condition_type,occurred_at,box_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (event_id, rule.id, rule.name, track["camera_id"], track["id"], moment.detection_id, track["label"],
                         rule.condition_type, moment.at.isoformat(), json.dumps(moment.box, separators=(",", ":"))),
                    ).rowcount
                    if inserted:
                        create_alarm(db, event_id)
            db.execute("UPDATE rules_cursor SET last_detection_id=? WHERE singleton_id=1", (fresh[-1]["id"],))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return len(fresh)


def _tamper_kinds(rule: Rule) -> list[str]:
    return [kind for kind in rule.condition.get("kinds") or TAMPER_KINDS if kind in TAMPER_KINDS]


def fire_tamper(db: sqlite3.Connection, camera_id: str, kind: str, at: datetime) -> int:
    """Raise an alarm for each ``tamper`` rule that watches the camera and the kind, inside the caller's transaction."""
    groups = _camera_groups(db, camera_id)
    fired = 0
    for rule in load_rules(db):
        if rule.condition_type != "tamper" or not rule.watches_camera(camera_id, groups) or kind not in _tamper_kinds(rule):
            continue
        if at < rule.active_from or not rule.scheduled(at):
            continue
        if rule.cooldown and db.execute("SELECT 1 FROM rule_events WHERE rule_id=? AND camera_id=? AND occurred_at>? AND occurred_at<? LIMIT 1",
                                        (rule.id, camera_id, (at - rule.cooldown).isoformat(), (at + rule.cooldown).isoformat())).fetchone():
            continue
        event_id = str(uuid.uuid4())
        db.execute("INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,detection_id,label,condition_type,occurred_at,box_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (event_id, rule.id, rule.name, camera_id, f"tamper-{event_id}", None, kind, "tamper", at.isoformat(), FULL_FRAME))
        create_alarm(db, event_id)
        fired += 1
    return fired


def _replay_tamper(rule: Rule, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """The tamper detections of [start, end) the rule would have fired on, from the incident history."""
    with connect() as db:
        rows = db.execute(
            "SELECT i.camera_id,i.fault_type,e.occurred_at,c.name AS camera_name FROM incident_events e JOIN incidents i ON i.id=e.incident_id "
            "JOIN cameras c ON c.id=i.camera_id WHERE i.fault_type LIKE 'tamper\\_%' ESCAPE '\\' AND e.event_type IN ('raised','recurred') "
            "AND e.occurred_at>=? AND e.occurred_at<? ORDER BY e.occurred_at,e.id", (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())).fetchall()
        groups = {row["camera_id"]: _camera_groups(db, row["camera_id"]) for row in rows}
    events, last = [], {}
    for row in rows:
        kind, at = row["fault_type"].removeprefix("tamper_"), _parse(row["occurred_at"])
        if not rule.watches_camera(row["camera_id"], groups[row["camera_id"]]) or kind not in _tamper_kinds(rule) or not rule.scheduled(at):
            continue
        if rule.cooldown and row["camera_id"] in last and abs(at - last[row["camera_id"]]) < rule.cooldown:
            continue
        last[row["camera_id"]] = at
        events.append({"camera_id": row["camera_id"], "camera_name": row["camera_name"], "track_id": None, "label": kind, "occurred_at": at.isoformat(),
                       "box": json.loads(FULL_FRAME)})
    return events


def _external_matches(rule: Rule, event: dict[str, Any]) -> bool:
    """Whether an external event is one the rule asks about: each list in the condition (sources, event types, subjects) is a filter, and an empty one lets everything through."""
    condition = rule.condition
    return ((not condition.get("source_ids") or event["source_id"] in condition["source_ids"]) and (not condition.get("types") or event["type"] in condition["types"])
            and (not condition.get("subjects") or event["subject"] in condition["subjects"]))


def fire_external(db: sqlite3.Connection, camera_id: str, event: dict[str, Any], at: datetime) -> int:
    """Raise an alarm for each ``external_event`` rule that watches the camera and asks about this event, inside the caller's transaction."""
    groups = _camera_groups(db, camera_id)
    fired = 0
    for rule in load_rules(db):
        if rule.condition_type != "external_event" or not rule.watches_camera(camera_id, groups) or not _external_matches(rule, event):
            continue
        if at < rule.active_from or not rule.scheduled(at):
            continue
        if rule.cooldown and db.execute("SELECT 1 FROM rule_events WHERE rule_id=? AND camera_id=? AND occurred_at>? AND occurred_at<? LIMIT 1",
                                        (rule.id, camera_id, (at - rule.cooldown).isoformat(), (at + rule.cooldown).isoformat())).fetchone():
            continue
        event_id = str(uuid.uuid4())
        db.execute("INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,detection_id,label,condition_type,occurred_at,box_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (event_id, rule.id, rule.name, camera_id, f"external-{event['id']}-{camera_id}", None, event["type"], "external_event", at.isoformat(), FULL_FRAME))
        create_alarm(db, event_id)
        fired += 1
    return fired


def _replay_external(rule: Rule, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """The external events of [start, end) the rule would have fired on, from those kept."""
    with connect() as db:
        rows = db.execute(
            "SELECT e.id,e.source_id,e.type,e.subject,e.occurred_at,k.camera_id,c.name AS camera_name FROM external_events e JOIN external_event_cameras k ON k.event_id=e.id "
            "JOIN cameras c ON c.id=k.camera_id WHERE e.occurred_at>=? AND e.occurred_at<? ORDER BY e.occurred_at,e.id",
            (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())).fetchall()
        groups = {row["camera_id"]: _camera_groups(db, row["camera_id"]) for row in rows}
    events, last = [], {}
    for row in rows:
        at = _parse(row["occurred_at"])
        if not rule.watches_camera(row["camera_id"], groups[row["camera_id"]]) or not _external_matches(rule, dict(row)) or at < rule.active_from or not rule.scheduled(at):
            continue
        if rule.cooldown and row["camera_id"] in last and abs(at - last[row["camera_id"]]) < rule.cooldown:
            continue
        last[row["camera_id"]] = at
        events.append({"camera_id": row["camera_id"], "camera_name": row["camera_name"], "track_id": None, "label": row["type"], "occurred_at": at.isoformat(), "box": json.loads(FULL_FRAME)})
    return events


def replay(rule: Rule, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """What the rule would have fired over stored tracks in [start, end); writes nothing."""
    if rule.condition_type == "tamper":
        return _replay_tamper(rule, start, end)
    if rule.condition_type == "external_event":
        return _replay_external(rule, start, end)
    start_text, end_text = start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
    with connect() as db:
        tracks = db.execute(
            "SELECT t.id,t.camera_id,t.label,c.name AS camera_name FROM ai_tracks t JOIN cameras c ON c.id=t.camera_id "
            "WHERE t.first_seen_at<? AND t.last_seen_at>=? ORDER BY t.first_seen_at,t.id", (end_text, start_text)).fetchall()
        groups: dict[str, set[str]] = {}
        candidates = []
        for track in tracks:
            camera_groups = groups.setdefault(track["camera_id"], _camera_groups(db, track["camera_id"]))
            if not rule.watches(track["camera_id"], camera_groups, track["label"]):
                continue
            # The whole track, as the worker sees it; only moments inside the window count.
            moments = eligible_moments(rule, _track_path(db, track["id"]), since=start, until=end)
            if moments:
                candidates.append((track, moments))
    fired: dict[str, list[datetime]] = {}
    events = []
    for track, moments in sorted(candidates, key=lambda item: (item[1][0].at, item[0]["id"])):
        times = fired.setdefault(track["camera_id"], [])
        moment = choose_moment(rule, moments, lambda at: any(abs(at - time) < rule.cooldown for time in times))
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
