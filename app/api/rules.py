"""Administrator-defined rules over detections and tracks, their events, and test mode."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Union

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from app import rule_actions
from app.principal import Administrator
from app.rules import Rule, replay
from app.scheduling import validate_schedule
from app.store import connect

router = APIRouter()

TEST_WINDOW = timedelta(hours=24)


class Zone(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def inside_frame(self) -> "Zone":
        # Tolerates only floating-point error from rounded editor values.
        if self.x + self.width > 1 + 1e-9 or self.y + self.height > 1 + 1e-9:
            raise ValueError("A zone must stay inside the frame")
        return self


class Line(BaseModel):
    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)
    x2: float = Field(ge=0, le=1)
    y2: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def has_length(self) -> "Line":
        if ((self.x2 - self.x1) ** 2 + (self.y2 - self.y1) ** 2) ** 0.5 < 0.02:
            raise ValueError("A line must be at least 2% of the frame long")
        return self


class ZoneEntry(BaseModel):
    type: Literal["zone_entry"]
    zone: Zone


class LineCrossing(BaseModel):
    type: Literal["line_crossing"]
    line: Line
    direction: Literal["both", "forward", "reverse"] = "both"


class Loitering(BaseModel):
    type: Literal["loitering"]
    zone: Zone
    dwell_seconds: int = Field(ge=1, le=3600)


class WrongWay(BaseModel):
    type: Literal["wrong_way"]
    zone: Zone
    direction: Literal["right", "left", "down", "up"]


class Tamper(BaseModel):
    """The camera is covered, moved or defocused (see Camera tamper detection); a rule on it needs no object classes."""
    type: Literal["tamper"]
    kinds: list[Literal["covered", "moved", "defocused"]] = Field(default_factory=lambda: ["covered", "moved", "defocused"], min_length=1, max_length=3)

    @field_validator("kinds")
    @classmethod
    def distinct(cls, value: list[str]) -> list[str]:
        return sorted(set(value), key=["covered", "moved", "defocused"].index)


class ExternalEvent(BaseModel):
    """An event from a building system (a door, an intercom, a sensor) tied to the camera; each list narrows it, and an empty one means any."""
    type: Literal["external_event"]
    source_ids: list[str] = Field(default_factory=list, max_length=50)
    types: list[str] = Field(default_factory=list, max_length=50)
    subjects: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("types", "subjects", "source_ids")
    @classmethod
    def trimmed_distinct(cls, value: list[str]) -> list[str]:
        return sorted({item.strip() for item in value if item.strip()})


Condition = Annotated[Union[ZoneEntry, LineCrossing, Loitering, WrongWay, Tamper, ExternalEvent], Field(discriminator="type")]


class ScheduleInterval(BaseModel):
    days: list[int]
    start: str
    end: str


class RuleSchedule(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)
    intervals: list[ScheduleInterval] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid(self) -> "RuleSchedule":
        validate_schedule(self.timezone, [{**interval.model_dump(), "mode": "continuous"} for interval in self.intervals])
        return self


class ActionDefinition(BaseModel):
    """One rule action. Secrets (url, signing_secret) are write-only: send them to set or
    change them, leave them out (with the action's id) to keep what is saved."""
    id: str | None = Field(default=None, max_length=64)
    type: Literal["webhook", "mqtt", "slack", "teams", "relay"]
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    url: str | None = Field(default=None, max_length=2048)
    signing_secret: str | None = Field(default=None, max_length=256)
    allow_private: bool = False
    topic: str | None = Field(default=None, max_length=1024)
    retain: bool = False
    camera_id: str | None = Field(default=None, max_length=64)
    relay_token: str | None = Field(default=None, max_length=128)
    duration_seconds: float = Field(default=1.0, ge=0.1, le=rule_actions.MAX_RELAY_SECONDS)

    @field_validator("name")
    @classmethod
    def trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter an action name")
        return value


class RuleDefinition(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    camera_ids: list[str] = Field(default_factory=list, max_length=500)
    group_ids: list[str] = Field(default_factory=list, max_length=200)
    condition: Condition
    classes: list[Literal["person", "vehicle"]] = Field(default_factory=list)
    schedule: RuleSchedule | None = None
    cooldown_seconds: int = Field(default=0, ge=0, le=86400)
    # Bring the alarm's camera and its neighbours into Live for people who opted in.
    pop_into_live: bool = False
    actions: list[ActionDefinition] = Field(default_factory=list, max_length=rule_actions.MAX_ACTIONS)

    @field_validator("name")
    @classmethod
    def trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a rule name")
        return value

    @model_validator(mode="after")
    def valid_definition(self) -> "RuleDefinition":
        if not self.camera_ids and not self.group_ids:
            raise ValueError("Choose at least one camera or group")
        if self.condition.type in ("tamper", "external_event"):
            self.classes = []
        elif not self.classes:
            raise ValueError("Choose at least one object class")
        return self


def _check_scope(db, definition: RuleDefinition) -> None:
    for table, ids, noun in (("cameras", definition.camera_ids, "camera"), ("camera_groups", definition.group_ids, "group")):
        for item in set(ids):
            if db.execute(f"SELECT 1 FROM {table} WHERE id=?", (item,)).fetchone() is None:
                raise HTTPException(status_code=422, detail=f"Unknown {noun} in the rule scope")


def _condition_json(definition: RuleDefinition) -> str:
    return json.dumps(definition.condition.model_dump(exclude={"type"}), separators=(",", ":"))


def _columns(definition: RuleDefinition) -> tuple[object, ...]:
    return (
        definition.name, int(definition.enabled), json.dumps(sorted(set(definition.camera_ids))), json.dumps(sorted(set(definition.group_ids))),
        definition.condition.type, _condition_json(definition), json.dumps(sorted(set(definition.classes))),
        json.dumps(definition.schedule.model_dump(), separators=(",", ":")) if definition.schedule else None, definition.cooldown_seconds,
        int(definition.pop_into_live),
    )


def rule_view(row, db) -> dict[str, object]:
    condition = {"type": row["condition_type"], **json.loads(row["condition_json"])}
    return {
        "id": row["id"], "name": row["name"], "enabled": bool(row["enabled"]),
        "camera_ids": json.loads(row["camera_ids_json"]), "group_ids": json.loads(row["group_ids_json"]),
        "condition": condition, "classes": json.loads(row["classes_json"]),
        "schedule": json.loads(row["schedule_json"]) if row["schedule_json"] else None,
        "cooldown_seconds": row["cooldown_seconds"], "pop_into_live": bool(row["pop_into_live"]), "active_from": row["active_from"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "actions": rule_actions.action_views(db, row["id"]),
    }


def _save_actions(db, rule_id: str, definition: RuleDefinition) -> None:
    try:
        rule_actions.save_actions(db, rule_id, [action.model_dump() for action in definition.actions])
    except rule_actions.ActionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _audit(db, actor: Administrator, action: str, rule_id: str, details: dict[str, object]) -> None:
    db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
               (int(actor["id"]), action, "rule", rule_id, json.dumps(details, separators=(",", ":"))))


def _nudge(request: Request) -> None:
    # Edits take effect at the rules worker's next pass; a nudge makes that now.
    rules = getattr(request.app.state, "rules", None)
    if rules is not None:
        rules.nudge()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/api/rules")
def list_rules(_: Administrator) -> dict[str, object]:
    with connect() as db:
        rows = db.execute("SELECT * FROM rules ORDER BY name COLLATE NOCASE,id").fetchall()
        return {"rules": [rule_view(row, db) for row in rows]}


@router.post("/api/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(definition: RuleDefinition, request: Request, user: Administrator) -> dict[str, object]:
    rule_id = str(uuid.uuid4())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _check_scope(db, definition)
            db.execute(
                "INSERT INTO rules(name,enabled,camera_ids_json,group_ids_json,condition_type,condition_json,classes_json,schedule_json,cooldown_seconds,pop_into_live,"
                "id,active_from,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (*_columns(definition), rule_id, _now(), int(user["id"])))
            _save_actions(db, rule_id, definition)
            _audit(db, user, "rule_created", rule_id, {"name": definition.name, "actions": len(definition.actions)})
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        row = db.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        view = rule_view(row, db)
    _nudge(request)
    return view


@router.get("/api/rules/{rule_id}")
def get_rule(rule_id: str, _: Administrator) -> dict[str, object]:
    with connect() as db:
        row = db.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        return rule_view(row, db)


@router.get("/api/rules/{rule_id}/deliveries")
def get_rule_deliveries(rule_id: str, _: Administrator, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]:
    """The rule's action deliveries, newest first: status, attempts and the error code of the last try."""
    with connect() as db:
        if db.execute("SELECT 1 FROM rules WHERE id=?", (rule_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Rule not found")
    return {"deliveries": rule_actions.deliveries(rule_id, limit)}


@router.put("/api/rules/{rule_id}")
async def update_rule(rule_id: str, definition: RuleDefinition, request: Request, user: Administrator) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT 1 FROM rules WHERE id=?", (rule_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="Rule not found")
            _check_scope(db, definition)
            now = _now()
            db.execute(
                "UPDATE rules SET name=?,enabled=?,camera_ids_json=?,group_ids_json=?,condition_type=?,condition_json=?,classes_json=?,schedule_json=?,cooldown_seconds=?,"
                "pop_into_live=?,active_from=?,updated_at=? WHERE id=?", (*_columns(definition), now, now, rule_id))
            _save_actions(db, rule_id, definition)
            _audit(db, user, "rule_updated", rule_id, {"name": definition.name, "enabled": definition.enabled, "actions": len(definition.actions)})
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        row = db.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        view = rule_view(row, db)
    _nudge(request)
    return view


@router.delete("/api/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(rule_id: str, user: Administrator) -> Response:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT name FROM rules WHERE id=?", (rule_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Rule not found")
            db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
            _audit(db, user, "rule_deleted", rule_id, {"name": row["name"]})
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/rules/test")
def test_rule(definition: RuleDefinition, _: Administrator) -> dict[str, object]:
    """Replay the stored detections of the last 24 hours through a rule, saved or not. Creates nothing."""
    end = datetime.now(timezone.utc)
    start = end - TEST_WINDOW
    with connect() as db:
        _check_scope(db, definition)
    rule = Rule(
        id="test", name=definition.name, enabled=definition.enabled, camera_ids=frozenset(definition.camera_ids), group_ids=frozenset(definition.group_ids),
        condition_type=definition.condition.type, condition=definition.condition.model_dump(exclude={"type"}), classes=frozenset(definition.classes),
        schedule=definition.schedule.model_dump() if definition.schedule else None, cooldown=timedelta(seconds=definition.cooldown_seconds), active_from=start,
    )
    events = replay(rule, start, end)
    # Listed, never run: test mode performs no action.
    actions = rule_actions.describe([action.model_dump() for action in definition.actions])
    return {"start": start.isoformat(), "end": end.isoformat(), "events": events, "count": len(events), "actions": actions,
            "coverage_note": "Test mode replays analysed frames only; time a camera was not analysed cannot match."}


@router.get("/api/rule-events")
def list_rule_events(
    _: Administrator,
    rule_id: str | None = Query(default=None, max_length=64),
    camera_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, object]:
    clauses, values = [], []
    for column, value in (("e.rule_id", rule_id), ("e.camera_id", camera_id)):
        if value:
            clauses.append(f"{column}=?")
            values.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as db:
        rows = db.execute(
            "SELECT e.*,c.name AS camera_name FROM rule_events e JOIN cameras c ON c.id=e.camera_id" + where + " ORDER BY e.occurred_at DESC,e.id LIMIT ?",
            (*values, limit)).fetchall()
    events = []
    for row in rows:
        item = dict(row)
        item["box"] = json.loads(item.pop("box_json"))
        events.append(item)
    return {"events": events}
