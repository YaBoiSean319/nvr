"""Rule actions: what a rule does beyond raising an alarm.

Each rule event queues one delivery per enabled action: a signed webhook (the NVR's
webhook signing scheme), an MQTT publish, a Slack or Microsoft Teams incoming-webhook
message, or an ONVIF relay pulse on a camera. Deliveries run in the background worker
with retries and are logged per rule. Destination URLs, MQTT credentials and signing
keys are encrypted with the credential key, never returned, and never logged: errors
are recorded as short codes.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import mqtt
from .credentials import decrypt, encrypt
from .store import connect
from .webhooks import signed_headers, validate_target_url

TYPES = ("webhook", "mqtt", "slack", "teams", "relay")
SECRET_TYPES = {"webhook", "mqtt", "slack", "teams"}
MAX_ACTIONS = 10
MAX_ATTEMPTS = 8
RETRY_SECONDS = (2, 5, 15, 60, 300, 1800, 3600)
KEEP_DELIVERIES = timedelta(days=30)
REQUEST_TIMEOUT_SECONDS = 10
MIN_SIGNING_SECRET = 16
MAX_RELAY_SECONDS = 60


class ActionError(ValueError):
    """An action cannot be saved as given."""


def validate_destination(url: str, allow_private: bool) -> str:
    """The webhook destination rules: public HTTPS, or an authorised literal private IP."""
    return validate_target_url(url, allow_private)


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")


def _check_broker(url: str, allow_private: bool) -> mqtt.Broker:
    broker = mqtt.parse_broker(url)
    # Same address rules as webhooks; a public broker must use TLS, as a public webhook must use HTTPS.
    validate_destination(f"{'https' if broker.tls else 'http'}://{broker.host}:{broker.port}/", allow_private)
    return broker


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings(action: dict[str, Any]) -> dict[str, Any]:
    kind = action["type"]
    if kind == "webhook":
        return {"allow_private": bool(action.get("allow_private"))}
    if kind == "mqtt":
        return {"allow_private": bool(action.get("allow_private")), "topic": mqtt.validate_topic(action.get("topic") or ""), "retain": bool(action.get("retain"))}
    if kind == "relay":
        duration = float(action.get("duration_seconds") or 1)
        if not 0.1 <= duration <= MAX_RELAY_SECONDS:
            raise ActionError(f"Hold the relay for 0.1 to {MAX_RELAY_SECONDS} seconds")
        token = (action.get("relay_token") or "").strip() or None
        return {"camera_id": action.get("camera_id"), "relay_token": token, "duration_seconds": duration}
    return {}


def _secret(kind: str, action: dict[str, Any], current: dict[str, str] | None, settings: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    """The action's secrets (merged with what is saved) and the display target."""
    if kind not in SECRET_TYPES:
        return None, None
    secret = dict(current or {})
    if action.get("url"):
        secret["url"] = action["url"].strip()
    if kind == "webhook" and action.get("signing_secret"):
        secret["signing_secret"] = action["signing_secret"]
    if not secret.get("url"):
        raise ActionError(f"Enter the destination for {action['name']}")
    if kind == "webhook" and len(secret.get("signing_secret", "")) < MIN_SIGNING_SECRET:
        raise ActionError(f"Enter a signing secret of at least {MIN_SIGNING_SECRET} characters for {action['name']}")
    try:
        if kind == "mqtt":
            broker = _check_broker(secret["url"], settings["allow_private"])
            return secret, f"{'mqtts' if broker.tls else 'mqtt'}://{broker.host}:{broker.port}"
        target = validate_destination(secret["url"], settings.get("allow_private", False))
    except ValueError as exc:
        raise ActionError(f"{action['name']}: {exc}") from exc
    return secret, _origin(target)


def save_actions(db: sqlite3.Connection, rule_id: str, actions: list[dict[str, Any]]) -> None:
    """Replace a rule's actions inside the caller's transaction. An action sent with its id
    keeps its saved secrets unless new ones are given; actions left out are removed."""
    if len(actions) > MAX_ACTIONS:
        raise ActionError(f"A rule runs at most {MAX_ACTIONS} actions")
    existing = {row["id"]: row for row in db.execute("SELECT * FROM rule_actions WHERE rule_id=?", (rule_id,))}
    kept: set[str] = set()
    for position, action in enumerate(actions):
        kind = action["type"]
        previous = existing.get(action.get("id") or "")
        current = json.loads(decrypt(previous["secret_ciphertext"])) if previous is not None and previous["secret_ciphertext"] and previous["type"] == kind else None
        try:
            settings = _settings(action)
        except ValueError as exc:
            raise ActionError(f"{action['name']}: {exc}") from exc
        if kind == "relay":
            camera = db.execute("SELECT device_service_url FROM cameras WHERE id=?", (settings["camera_id"],)).fetchone()
            if camera is None:
                raise ActionError(f"{action['name']}: choose a camera")
            if not camera["device_service_url"]:
                raise ActionError(f"{action['name']}: the camera has no ONVIF device service for relay outputs")
        secret, target = _secret(kind, action, current, settings)
        values = (position, kind, action["name"], int(action.get("enabled", True)), json.dumps(settings, separators=(",", ":")),
                  encrypt(json.dumps(secret)) if secret else None, target, _now().isoformat())
        if previous is not None:
            db.execute("UPDATE rule_actions SET position=?,type=?,name=?,enabled=?,settings_json=?,secret_ciphertext=?,target=?,updated_at=? WHERE id=?",
                       (*values, previous["id"]))
            kept.add(previous["id"])
        else:
            db.execute("INSERT INTO rule_actions(position,type,name,enabled,settings_json,secret_ciphertext,target,updated_at,id,rule_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (*values, str(uuid.uuid4()), rule_id))
    for action_id in set(existing) - kept:
        db.execute("DELETE FROM rule_actions WHERE id=?", (action_id,))


def _view(row: sqlite3.Row) -> dict[str, Any]:
    settings = json.loads(row["settings_json"])
    secret_keys = set(json.loads(decrypt(row["secret_ciphertext"]))) if row["secret_ciphertext"] else set()
    view: dict[str, Any] = {"id": row["id"], "type": row["type"], "name": row["name"], "enabled": bool(row["enabled"]), "target": row["target"], **settings}
    if row["type"] == "webhook":
        view["has_signing_secret"] = "signing_secret" in secret_keys
    return view


def action_views(db: sqlite3.Connection, rule_id: str) -> list[dict[str, Any]]:
    return [_view(row) for row in db.execute("SELECT * FROM rule_actions WHERE rule_id=? ORDER BY position", (rule_id,))]


def describe(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What test mode lists: each enabled action's type and name, and where it would go. Runs nothing."""
    described = []
    for action in actions:
        if not action.get("enabled", True):
            continue
        target = None
        if action.get("url") and action["type"] in SECRET_TYPES:
            try:
                target = mqtt_target(action["url"]) if action["type"] == "mqtt" else _origin(action["url"])
            except ValueError:
                target = None
        described.append({"type": action["type"], "name": action["name"], "target": target})
    return described


def mqtt_target(url: str) -> str:
    broker = mqtt.parse_broker(url)
    return f"{'mqtts' if broker.tls else 'mqtt'}://{broker.host}:{broker.port}"


def _public_url() -> str:
    return os.environ.get("NVR_PUBLIC_URL", "").strip().rstrip("/")


def _payload(db: sqlite3.Connection, rule_event_id: str, alarm_id: str | None) -> dict[str, Any]:
    from .push import thumbnail_token

    event = db.execute("SELECT * FROM rule_events WHERE id=?", (rule_event_id,)).fetchone()
    camera = db.execute("SELECT name FROM cameras WHERE id=?", (event["camera_id"],)).fetchone()
    public = _public_url()
    return {
        "event_id": rule_event_id, "event_type": "rule.triggered", "occurred_at": event["occurred_at"],
        "rule_id": event["rule_id"], "rule_name": event["rule_name"], "camera_id": event["camera_id"],
        "camera_name": camera["name"] if camera else None, "label": event["label"], "condition_type": event["condition_type"],
        "alarm_id": alarm_id,
        "alarm_url": f"{public}/?alarm={alarm_id}" if public and alarm_id else None,
        # A short-lived link that names only this alarm, like a push notification's picture.
        "thumbnail_url": f"{public}/api/push/thumbnails/{thumbnail_token(alarm_id)}" if public and alarm_id else None,
    }


def queue_for_event(db: sqlite3.Connection, rule_event_id: str, alarm_id: str | None) -> int:
    """Queue one delivery per enabled action of the event's rule, inside the caller's transaction."""
    event = db.execute("SELECT rule_id FROM rule_events WHERE id=?", (rule_event_id,)).fetchone()
    if event is None or event["rule_id"] is None:
        return 0
    actions = db.execute("SELECT id,name,type FROM rule_actions WHERE rule_id=? AND enabled=1 ORDER BY position", (event["rule_id"],)).fetchall()
    if not actions:
        return 0
    payload = json.dumps(_payload(db, rule_event_id, alarm_id), sort_keys=True, separators=(",", ":"))
    now = _now().isoformat()
    queued = 0
    for action in actions:
        queued += db.execute(
            "INSERT OR IGNORE INTO rule_action_deliveries(id,rule_id,action_id,action_name,action_type,rule_event_id,alarm_id,payload_json,next_attempt_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), event["rule_id"], action["id"], action["name"], action["type"], rule_event_id, alarm_id, payload, now, now, now),
        ).rowcount
    return queued


def deliveries(rule_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM rule_action_deliveries WHERE rule_id=? ORDER BY created_at DESC, id LIMIT ?", (rule_id, min(max(limit, 1), 500))).fetchall()
    return [{key: row[key] for key in ("id", "action_id", "action_name", "action_type", "rule_event_id", "alarm_id", "status", "attempt_count",
                                        "next_attempt_at", "response_status", "last_error", "created_at", "updated_at", "delivered_at")} for row in rows]


def recover_inflight() -> None:
    with connect() as db:
        db.execute("UPDATE rule_action_deliveries SET status='queued',next_attempt_at=?,last_error='process_restarted' WHERE status='sending'", (_now().isoformat(),))


def _claim() -> dict[str, Any] | None:
    now = _now().isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT d.*, a.settings_json, a.secret_ciphertext, a.enabled AS action_enabled FROM rule_action_deliveries d "
                "LEFT JOIN rule_actions a ON a.id=d.action_id WHERE d.status='queued' AND d.next_attempt_at<=? ORDER BY d.next_attempt_at, d.created_at LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            attempt = int(row["attempt_count"]) + 1
            db.execute("UPDATE rule_action_deliveries SET status='sending',attempt_count=?,updated_at=? WHERE id=?", (attempt, now, row["id"]))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {**dict(row), "attempt_count": attempt}


def _finish(delivery_id: str, attempt: int, *, response_status: int | None = None, error: str | None = None, retryable: bool = False) -> None:
    now = _now()
    with connect() as db:
        if error is None:
            db.execute("UPDATE rule_action_deliveries SET status='delivered',response_status=?,last_error=NULL,updated_at=?,delivered_at=? WHERE id=?",
                       (response_status, now.isoformat(), now.isoformat(), delivery_id))
        else:
            retry = retryable and attempt < MAX_ATTEMPTS
            next_at = now + timedelta(seconds=RETRY_SECONDS[min(attempt - 1, len(RETRY_SECONDS) - 1)])
            db.execute("UPDATE rule_action_deliveries SET status=?,response_status=?,last_error=?,next_attempt_at=?,updated_at=? WHERE id=?",
                       ("queued" if retry else "failed", response_status, error, next_at.isoformat(), now.isoformat(), delivery_id))
        db.execute("DELETE FROM rule_action_deliveries WHERE status IN ('delivered','failed') AND updated_at<?", ((now - KEEP_DELIVERIES).isoformat(),))


def _message(payload: dict[str, Any]) -> str:
    when = payload["occurred_at"][11:19]
    text = f"{payload['rule_name']}: {payload['label'].capitalize()} on {payload['camera_name'] or 'a camera'} at {when} UTC"
    return text


async def _post(url: str, body: bytes, headers: dict[str, str], allow_private: bool) -> int:
    target = await asyncio.wait_for(asyncio.to_thread(validate_destination, url, allow_private), timeout=5)
    async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=3), follow_redirects=False, trust_env=False) as client:
        response = await client.post(target, content=body, headers=headers)
    return int(response.status_code)


async def _perform(delivery: dict[str, Any]) -> tuple[int | None, str | None, bool]:
    """(response status, error code, retryable). Error codes never carry a secret or a URL."""
    kind = delivery["action_type"]
    settings = json.loads(delivery["settings_json"] or "{}")
    secret = json.loads(decrypt(delivery["secret_ciphertext"])) if delivery["secret_ciphertext"] else {}
    payload = json.loads(delivery["payload_json"])
    try:
        if kind in {"webhook", "slack", "teams"}:
            if kind == "webhook":
                body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                headers = signed_headers(secret["signing_secret"], payload["event_id"], "rule.triggered", body)
            else:
                text = _message(payload)
                if kind == "slack":
                    links = [f"<{payload[key]}|{label}>" for key, label in (("thumbnail_url", "Snapshot"), ("alarm_url", "Open alarm")) if payload.get(key)]
                else:
                    links = [f"[{label}]({payload[key]})" for key, label in (("thumbnail_url", "Snapshot"), ("alarm_url", "Open alarm")) if payload.get(key)]
                body = json.dumps({"text": " · ".join([text, *links])}).encode("utf-8")
                headers = {"Content-Type": "application/json"}
            status = await _post(secret["url"], body, headers, bool(settings.get("allow_private")))
            if 200 <= status < 300:
                return status, None, False
            return status, f"http_{status}", status == 429 or status >= 500
        if kind == "mqtt":
            broker = await asyncio.wait_for(asyncio.to_thread(_check_broker, secret["url"], bool(settings.get("allow_private"))), timeout=5)
            await mqtt.publish(broker, settings["topic"], json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                               qos=1, retain=bool(settings.get("retain")))
            return None, None, False
        if kind == "relay":
            from .onvif_relay import pulse

            with connect() as db:
                camera = db.execute("SELECT device_service_url,motion_credentials_ciphertext FROM cameras WHERE id=?", (settings.get("camera_id"),)).fetchone()
            if camera is None or not camera["device_service_url"]:
                return None, "relay_camera_unavailable", False
            credentials = json.loads(decrypt(camera["motion_credentials_ciphertext"])) if camera["motion_credentials_ciphertext"] else {}
            await pulse(camera["device_service_url"], credentials.get("username", ""), credentials.get("password", ""),
                        settings.get("relay_token"), float(settings.get("duration_seconds", 1)))
            return None, None, False
        return None, "unknown_action", False
    except mqtt.MqttRefused as exc:
        return None, f"mqtt_refused_{exc.code}", False
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        return code, f"relay_http_{code}", code not in {400, 401, 403, 404}
    except LookupError:
        return None, "relay_no_outputs", False
    except ValueError:
        return None, "invalid_destination", False
    except (OSError, asyncio.TimeoutError, httpx.TransportError, ConnectionError) as exc:
        return None, f"transport_{type(exc).__name__}", True


async def deliver_one() -> bool:
    delivery = await asyncio.to_thread(_claim)
    if delivery is None:
        return False
    attempt = int(delivery["attempt_count"])
    if delivery["action_id"] is None or not delivery["action_enabled"]:
        await asyncio.to_thread(_finish, delivery["id"], attempt, error="action_removed_or_disabled")
        return True
    status, error, retryable = await _perform(delivery)
    await asyncio.to_thread(_finish, delivery["id"], attempt, response_status=status, error=error, retryable=retryable)
    return True


async def deliver_due() -> int:
    delivered = 0
    while await deliver_one():
        delivered += 1
    return delivered


class RuleActionManager:
    def __init__(self, poll_seconds: float = 1.0) -> None:
        self.poll_seconds = poll_seconds
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await asyncio.to_thread(recover_inflight)
        self.task = asyncio.create_task(self._run(), name="rule-action-worker")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _run(self) -> None:
        while True:
            try:
                if not await deliver_one():
                    await asyncio.sleep(self.poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(self.poll_seconds)
