"""External events in (#138): events from building systems (doors, intercoms, panic buttons, sensors), tied to cameras.

An event reaches the NVR through a **source**: a signed HTTP endpoint, an MQTT subscription, or Home Assistant's event topic. It says what happened
(``type``), to what (``subject``: a door, a sensor), when, and optionally a message and data. An administrator **maps** a subject to cameras; a mapped
event is kept against those cameras, so it shows on their Playback timeline and can raise alarms through ``external_event`` rules; an event nothing is
mapped to is still kept and listed for the administrator, so a mapping can be written from what really arrives.

HTTP events are signed like the NVR's own webhooks: HMAC-SHA256 over "<timestamp>.<body>" with the source's secret. An unsigned, wrongly signed, stale or
repeated one is refused, and an unknown or switched-off source is refused in the same way, so nothing says which sources exist. MQTT events are trusted on the
broker's login and topic permissions. Secrets and broker logins are encrypted and never returned.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import mqtt, rule_actions, rules
from .credentials import decrypt, encrypt
from .store import connect

logger = logging.getLogger("nvr.external_events")

MAX_BODY_BYTES = 16 * 1024
MAX_DATA_BYTES = 4 * 1024
SIGNATURE_WINDOW_SECONDS = 300
FUTURE_SLACK = timedelta(minutes=5)
MAX_AGE = timedelta(days=7)
KEEP_EVENTS = timedelta(days=90)
MIN_SECRET = 16
MAX_SOURCES = 50
MAX_MAPPINGS_PER_SOURCE = 500
MAX_MESSAGE = 500
TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
HOME_ASSISTANT_SOURCE = "home-assistant"
RECONCILE_SECONDS = 5.0
LABELS = {"door_granted": "Door: access granted", "door_denied": "Door: access denied", "door_forced": "Door forced open", "door_held_open": "Door held open",
          "intercom_call": "Intercom call", "panic": "Panic button", "sensor": "Sensor"}


class SourceError(ValueError):
    """A source or mapping cannot be saved as given."""


class EventRefused(Exception):
    """An inbound event was not accepted. ``status`` is the HTTP answer; ``reason`` is for the log, never the sender."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def label(type_: str) -> str:
    return LABELS.get(type_, type_.replace("_", " ").replace(".", " ").capitalize())


# --- signatures ---------------------------------------------------------------------------------------------------------------------------

def sign(secret: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    """The headers a sender adds: the timestamp (seconds) and ``sha256=`` of HMAC-SHA256 over "<timestamp>.<body>" with the source's secret."""
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(secret.encode("utf-8"), stamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return {"X-NVR-Timestamp": stamp, "X-NVR-Signature": f"sha256={digest}"}


def verify_signature(secret: str, body: bytes, timestamp: str | None, signature: str | None, now: float | None = None) -> bool:
    """Whether the signature is right and the timestamp is within five minutes of now (a stale request is refused even if it was once valid)."""
    if not timestamp or not signature or not signature.startswith("sha256="):
        return False
    try:
        stamp = int(timestamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - stamp) > SIGNATURE_WINDOW_SECONDS:
        return False
    expected = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii", errors="replace") + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


# --- events -------------------------------------------------------------------------------------------------------------------------------

def parse_event(payload: Any, *, now: datetime, default_subject: str = "") -> dict[str, Any]:
    """An event from its JSON: ``type`` is required; ``subject``, ``message``, ``occurred_at``, ``data`` and ``event_id`` are optional. EventRefused (422) if it is not."""
    if not isinstance(payload, dict):
        raise EventRefused(422, "The event must be a JSON object")
    kind = str(payload.get("type", "")).strip().lower()
    if not TYPE_PATTERN.match(kind):
        raise EventRefused(422, "type is required: letters, digits and _ . : - up to 64 characters")
    subject = str(payload.get("subject", default_subject) or default_subject).strip()[:128]
    message = str(payload.get("message", "") or "").strip()[:MAX_MESSAGE]
    event_id = str(payload.get("event_id", "") or "").strip()[:128]
    data = payload.get("data", {})
    if not isinstance(data, dict) or len(json.dumps(data)) > MAX_DATA_BYTES:
        raise EventRefused(422, f"data must be an object of at most {MAX_DATA_BYTES} bytes")
    occurred = now
    if payload.get("occurred_at"):
        try:
            occurred = _parse(str(payload["occurred_at"]))
        except ValueError:
            raise EventRefused(422, "occurred_at must be a timestamp") from None
        if occurred > now + FUTURE_SLACK or occurred < now - MAX_AGE:
            raise EventRefused(422, "occurred_at must be within the last 7 days and not in the future")
    return {"event_id": event_id, "type": kind, "subject": subject, "message": message, "data": data, "occurred_at": occurred}


def mapped_cameras(db: sqlite3.Connection, source_id: str, subject: str, kind: str) -> list[str]:
    """The live cameras this source's mappings tie an event of this subject and type to."""
    cameras: list[str] = []
    for row in db.execute("SELECT subject,types_json,camera_ids_json FROM event_mappings WHERE source_id=? AND (subject=? OR subject='*')", (source_id, subject)):
        types = json.loads(row["types_json"])
        if types and kind not in types:
            continue
        cameras.extend(json.loads(row["camera_ids_json"]))
    if not cameras:
        return []
    marks = ",".join("?" * len(set(cameras)))
    live = {row[0] for row in db.execute(f"SELECT id FROM cameras WHERE archived_at IS NULL AND id IN ({marks})", tuple(set(cameras)))}
    return [camera for camera in dict.fromkeys(cameras) if camera in live]


def ingest(db: sqlite3.Connection, source: sqlite3.Row, event: dict[str, Any], *, now: datetime | None = None, fallback_id: str | None = None) -> dict[str, Any]:
    """Keep an event, tie it to its cameras and let rules react, inside the caller's transaction. EventRefused (409) if the source already sent that event id."""
    now = now or _now()
    row_id = str(uuid.uuid4())
    event_id = event["event_id"] or fallback_id or row_id
    try:
        db.execute("INSERT INTO external_events(id,source_id,source_name,event_id,type,subject,message,data_json,occurred_at,received_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (row_id, source["id"], source["name"], event_id, event["type"], event["subject"], event["message"], json.dumps(event["data"], separators=(",", ":")),
                    _iso(event["occurred_at"]), _iso(now)))
    except sqlite3.IntegrityError:
        raise EventRefused(409, "That event was already received") from None
    cameras = mapped_cameras(db, source["id"], event["subject"], event["type"])
    for camera_id in cameras:
        db.execute("INSERT OR IGNORE INTO external_event_cameras(event_id,camera_id) VALUES(?,?)", (row_id, camera_id))
    fired = sum(rules.fire_external(db, camera_id, {"id": row_id, "source_id": source["id"], "type": event["type"], "subject": event["subject"], "message": event["message"]},
                                    event["occurred_at"]) for camera_id in cameras)
    db.execute("UPDATE event_sources SET last_event_at=?,last_error=NULL WHERE id=?", (_iso(now), source["id"]))
    db.execute("DELETE FROM external_events WHERE received_at<?", (_iso(now - KEEP_EVENTS),))
    return {"id": row_id, "cameras": cameras, "alarms": fired}


def receive_http(source_id: str, body: bytes, headers: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Accept a signed event for an HTTP source. Everything wrong with the sender is answered 401 alike, so nothing says which sources exist."""
    if len(body) > MAX_BODY_BYTES:
        raise EventRefused(413, "The event is too large")
    now = now or _now()
    with connect() as db:
        source = db.execute("SELECT * FROM event_sources WHERE id=? AND kind='http' AND enabled=1", (source_id,)).fetchone()
        secret = decrypt(source["secret_ciphertext"]) if source is not None and source["secret_ciphertext"] else ""
    # An unknown source is checked against a secret that cannot match, so the work done is the same.
    if not verify_signature(secret or uuid.uuid4().hex, body, headers.get("x-nvr-timestamp"), headers.get("x-nvr-signature"), now.timestamp()) or source is None:
        raise EventRefused(401, "Not signed, or signed wrongly, or stale")
    try:
        event = parse_event(json.loads(body.decode("utf-8")), now=now)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EventRefused(422, "The event must be JSON") from None
    signature = str(headers.get("x-nvr-signature"))
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            result = ingest(db, source, event, now=now, fallback_id=hashlib.sha256(signature.encode()).hexdigest()[:32])  # No event id: the signature is the identity, so a replay is refused
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return result


def receive_message(source_id: str, topic: str, payload: bytes, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Accept a message from an MQTT source. JSON that is an event is used as it is; anything else becomes a generic event about the topic. None if it is refused."""
    now = now or _now()
    try:
        document: Any = json.loads(payload.decode("utf-8"))
        event = parse_event(document, now=now, default_subject=topic) if isinstance(document, dict) and document.get("type") else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        document, event = None, None
    except EventRefused as exc:
        logger.info("An MQTT event from %s was refused: %s", source_id, exc.reason)
        return None
    if event is None:
        event = {"event_id": "", "type": "mqtt_message", "subject": topic[:128], "message": payload.decode("utf-8", errors="replace")[:MAX_MESSAGE], "data": {}, "occurred_at": now}
    with connect() as db:
        source = db.execute("SELECT * FROM event_sources WHERE id=? AND enabled=1 AND kind IN ('mqtt','home_assistant')", (source_id,)).fetchone()
        if source is None:
            return None
        db.execute("BEGIN IMMEDIATE")
        try:
            result = ingest(db, source, event, now=now)
        except EventRefused:
            db.execute("ROLLBACK")
            return None
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return result


# --- sources and mappings (administrators) --------------------------------------------------------------------------------------------------

def _check_filter(value: str) -> str:
    try:
        return mqtt.validate_filter(value)
    except ValueError as exc:
        raise SourceError(str(exc)) from exc


def _validated(db: sqlite3.Connection, fields: dict[str, Any], current: sqlite3.Row | None) -> tuple[str, bytes | None, dict[str, Any], str | None]:
    """(kind, secret ciphertext, settings, display target) for a source being saved."""
    kind = fields["kind"] if current is None else current["kind"]
    settings: dict[str, Any] = {}
    secret = current["secret_ciphertext"] if current is not None else None
    target = current["target"] if current is not None else None
    if kind == "http":
        if fields.get("secret"):
            if len(fields["secret"]) < MIN_SECRET:
                raise SourceError(f"Enter a secret of at least {MIN_SECRET} characters")
            secret = encrypt(fields["secret"])
        if secret is None:
            raise SourceError(f"Enter a secret of at least {MIN_SECRET} characters")
        return kind, secret, settings, None
    if kind == "mqtt":
        previous = json.loads(current["settings_json"]) if current is not None else {}
        settings = {"topic": _check_filter(fields.get("topic") or previous.get("topic") or ""), "allow_private": bool(fields.get("allow_private", previous.get("allow_private", False)))}
        if fields.get("url"):
            try:
                rule_actions._check_broker(fields["url"].strip(), settings["allow_private"])
            except ValueError as exc:
                raise SourceError(str(exc)) from exc
            secret, target = encrypt(fields["url"].strip()), rule_actions.mqtt_target(fields["url"].strip())
        if secret is None:
            raise SourceError("Enter the broker as mqtt://host:1883 or mqtts://host:8883")
        return kind, secret, settings, target
    raise SourceError("Home Assistant is set up under Home Assistant, not as a source")


def save_source(db: sqlite3.Connection, fields: dict[str, Any], *, source_id: str | None = None, actor_id: int | None = None) -> str:
    """Create (or, with ``source_id``, change) a source inside the caller's transaction. A secret or broker address left out is kept as saved."""
    current = db.execute("SELECT * FROM event_sources WHERE id=?", (source_id,)).fetchone() if source_id else None
    if source_id and current is None:
        raise SourceError("Event source not found")
    if source_id is None and db.execute("SELECT COUNT(*) FROM event_sources").fetchone()[0] >= MAX_SOURCES:
        raise SourceError(f"At most {MAX_SOURCES} event sources")
    name = str(fields["name"]).strip()
    if not name:
        raise SourceError("Enter a name for the source")
    kind, secret, settings, target = _validated(db, fields, current)
    if current is not None:
        db.execute("UPDATE event_sources SET name=?,enabled=?,secret_ciphertext=?,settings_json=?,target=?,updated_at=? WHERE id=?",
                   (name, int(fields.get("enabled", True)), secret, json.dumps(settings), target, _iso(_now()), source_id))
        return source_id  # type: ignore[return-value]
    new_id = str(uuid.uuid4())
    db.execute("INSERT INTO event_sources(id,name,kind,enabled,secret_ciphertext,settings_json,target,created_by) VALUES(?,?,?,?,?,?,?,?)",
               (new_id, name, kind, int(fields.get("enabled", True)), secret, json.dumps(settings), target, actor_id))
    return new_id


def source_view(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """A source as the administrator sees it: never its secret or broker login."""
    settings = json.loads(row["settings_json"])
    return {"id": row["id"], "name": row["name"], "kind": row["kind"], "enabled": bool(row["enabled"]), "target": row["target"], "topic": settings.get("topic"),
            "allow_private": bool(settings.get("allow_private")), "has_secret": row["secret_ciphertext"] is not None, "last_event_at": row["last_event_at"], "last_error": row["last_error"],
            "created_at": row["created_at"], "mappings": mapping_views(db, row["id"]), "events": db.execute("SELECT COUNT(*) FROM external_events WHERE source_id=?", (row["id"],)).fetchone()[0]}


def mapping_views(db: sqlite3.Connection, source_id: str) -> list[dict[str, Any]]:
    return [{"id": row["id"], "subject": row["subject"], "types": json.loads(row["types_json"]), "camera_ids": json.loads(row["camera_ids_json"])}
            for row in db.execute("SELECT * FROM event_mappings WHERE source_id=? ORDER BY subject", (source_id,))]


def save_mappings(db: sqlite3.Connection, source_id: str, mappings: list[dict[str, Any]]) -> None:
    """Replace a source's mappings inside the caller's transaction."""
    if len(mappings) > MAX_MAPPINGS_PER_SOURCE:
        raise SourceError(f"At most {MAX_MAPPINGS_PER_SOURCE} mappings for a source")
    seen: set[str] = set()
    known = {row[0] for row in db.execute("SELECT id FROM cameras WHERE archived_at IS NULL")}
    rows = []
    for item in mappings:
        subject = str(item["subject"]).strip()[:128]
        if not subject:
            raise SourceError("Enter the subject (the door, sensor or button as the other system names it), or * for all")
        if subject in seen:
            raise SourceError(f"{subject} is mapped twice")
        seen.add(subject)
        cameras = list(dict.fromkeys(item["camera_ids"]))
        if not cameras or not set(cameras) <= known:
            raise SourceError(f"Choose the cameras for {subject}")
        types = sorted({str(kind).strip().lower() for kind in item.get("types") or []})
        if any(not TYPE_PATTERN.match(kind) for kind in types):
            raise SourceError("Event types are letters, digits and _ . : -")
        rows.append((str(uuid.uuid4()), source_id, subject, json.dumps(types), json.dumps(cameras)))
    db.execute("DELETE FROM event_mappings WHERE source_id=?", (source_id,))
    db.executemany("INSERT INTO event_mappings(id,source_id,subject,types_json,camera_ids_json) VALUES(?,?,?,?,?)", rows)


def recent(db: sqlite3.Connection, *, camera_ids: list[str] | None = None, source_id: str | None = None, start: datetime | None = None, end: datetime | None = None,
           limit: int = 200) -> list[dict[str, Any]]:
    """Events, newest first: those tied to the given cameras (or, for administrators with ``camera_ids`` None, all of them, mapped or not)."""
    clauses, values = [], []
    if camera_ids is not None:
        if not camera_ids:
            return []
        clauses.append(f"e.id IN (SELECT event_id FROM external_event_cameras WHERE camera_id IN ({','.join('?' * len(camera_ids))}))")
        values.extend(camera_ids)
    if source_id:
        clauses.append("e.source_id=?")
        values.append(source_id)
    if start:
        clauses.append("e.occurred_at>=?")
        values.append(_iso(start))
    if end:
        clauses.append("e.occurred_at<?")
        values.append(_iso(end))
    rows = db.execute(f"SELECT e.* FROM external_events e {'WHERE ' + ' AND '.join(clauses) if clauses else ''} ORDER BY e.occurred_at DESC,e.id LIMIT ?", (*values, limit)).fetchall()
    cameras: dict[str, list[str]] = {}
    if rows:
        for row in db.execute(f"SELECT event_id,camera_id FROM external_event_cameras WHERE event_id IN ({','.join('?' * len(rows))})", tuple(row["id"] for row in rows)):
            cameras.setdefault(row["event_id"], []).append(row["camera_id"])
    return [{"id": row["id"], "source_id": row["source_id"], "source_name": row["source_name"], "type": row["type"], "label": label(row["type"]), "subject": row["subject"],
             "message": row["message"], "occurred_at": row["occurred_at"], "received_at": row["received_at"],
             "camera_ids": [camera for camera in cameras.get(row["id"], []) if camera_ids is None or camera in camera_ids], "mapped": row["id"] in cameras} for row in rows]


# --- MQTT listeners -----------------------------------------------------------------------------------------------------------------------

def _listen_targets(db: sqlite3.Connection) -> dict[str, tuple[str, mqtt.Broker, list[str]]]:
    """What should be listened to now: source id -> (what identifies the configuration, broker, topic filters)."""
    targets: dict[str, tuple[str, mqtt.Broker, list[str]]] = {}
    for row in db.execute("SELECT * FROM event_sources WHERE kind='mqtt' AND enabled=1 AND secret_ciphertext IS NOT NULL"):
        try:
            url, topic = decrypt(row["secret_ciphertext"]), json.loads(row["settings_json"])["topic"]
            targets[row["id"]] = (f"{row['updated_at']}", mqtt.parse_broker(url), [topic])
        except (ValueError, KeyError):
            continue
    settings = db.execute("SELECT * FROM home_assistant_settings WHERE singleton_id=1").fetchone()
    if settings["enabled"] and settings["broker_ciphertext"]:
        try:
            targets[HOME_ASSISTANT_SOURCE] = (f"{settings['updated_at']}", mqtt.parse_broker(decrypt(settings["broker_ciphertext"])), [settings["events_topic"].rstrip("/") + "/#", settings["events_topic"]])
        except ValueError:
            pass
    return targets


def _record_error(source_id: str, text: str | None) -> None:
    with connect() as db:
        db.execute("UPDATE event_sources SET last_error=? WHERE id=?", (text, source_id))


class ExternalEventManager:
    """Keeps one MQTT subscription for each enabled MQTT source (and Home Assistant's event topic), restarting it when its settings change."""

    def __init__(self, reconcile_seconds: float = RECONCILE_SECONDS) -> None:
        self.reconcile_seconds = reconcile_seconds
        self.task: asyncio.Task[None] | None = None
        self.listeners: dict[str, tuple[str, asyncio.Task[None]]] = {}

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="external-events")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        await self._stop_all()

    async def _stop_all(self) -> None:
        tasks = [task for _, task in self.listeners.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.listeners.clear()

    async def reconcile(self) -> None:
        def read() -> dict[str, tuple[str, mqtt.Broker, list[str]]]:
            with connect() as db:
                return _listen_targets(db)
        wanted = await asyncio.to_thread(read)
        for source_id in list(self.listeners):
            signature, task = self.listeners[source_id]
            if source_id not in wanted or wanted[source_id][0] != signature or task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                del self.listeners[source_id]
        for source_id, (signature, broker, filters) in wanted.items():
            if source_id not in self.listeners:
                self.listeners[source_id] = (signature, asyncio.create_task(self._listen(source_id, broker, filters), name=f"events-{source_id}"))

    async def _listen(self, source_id: str, broker: mqtt.Broker, filters: list[str]) -> None:
        delay = 2.0

        async def on_message(topic: str, payload: bytes) -> None:
            await asyncio.to_thread(receive_message, source_id, topic, payload)

        while True:
            try:
                await mqtt.subscribe(broker, filters, on_message)
                error = "The broker closed the connection"
            except asyncio.CancelledError:
                raise
            except mqtt.MqttRefused as exc:
                error, delay = f"The broker refused the login ({exc.code})", 60.0
            except (OSError, ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
                error = f"The broker could not be reached ({type(exc).__name__})"
            await asyncio.to_thread(_record_error, source_id, error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def _run(self) -> None:
        while True:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Event source reconciliation failed")
            await asyncio.sleep(self.reconcile_seconds)
