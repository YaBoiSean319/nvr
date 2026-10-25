from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .credentials import decrypt, encrypt
from .store import connect

EVENT_TYPES = {
    "recording.failure",
    "recording.uncertain",
    "recording.recovered",
    "storage.failure",
    "storage.recovered",
    "ai.detection",
    "ai.failure",
    "ai.recovered",
    "system.failure",
    "system.recovered",
    "alarm.created",
    "alarm.acknowledged",
    "alarm.assigned",
    "alarm.resolved",
    "coverage.weekly_report",
    "webhook.test",
}
# httpx logs each request's full URL at INFO. Webhook, Slack and Teams URLs carry their
# secret in the path, so outbound request logging stays at warnings and above.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

MAX_ATTEMPTS = 8
MAX_PENDING_PER_ENDPOINT = 1000
REQUEST_TIMEOUT_SECONDS = 10
RETRY_SECONDS = (2, 5, 15, 60, 300, 1800, 3600)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_target_url(value: str, allow_private: bool) -> str:
    target = value.strip()
    try:
        parsed = urlsplit(target)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Webhook URL is invalid") from exc
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Webhook URL must be an HTTP(S) destination without embedded credentials or a fragment")
    host = parsed.hostname.rstrip(".")
    if not host or any(ord(char) < 33 for char in host):
        raise ValueError("Webhook host is invalid")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Webhook port is invalid")
    literal_host = True
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        literal_host = False
        try:
            resolved = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("Webhook hostname could not be resolved") from exc
        addresses = list({ipaddress.ip_address(item[4][0]) for item in resolved})
    if not addresses:
        raise ValueError("Webhook hostname has no usable address")
    for address in addresses:
        if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved:
            raise ValueError("Loopback, link-local, multicast, and reserved webhook destinations are not allowed")
    private_kinds = [address.is_private for address in addresses]
    if any(not address.is_global and not address.is_private for address in addresses):
        raise ValueError("Non-public special-purpose webhook destinations are not allowed")
    if any(private_kinds) and not literal_host:
        raise ValueError("Private webhook receivers must use an explicit private IP address")
    if any(private_kinds) and (not allow_private or not all(private_kinds)):
        raise ValueError("Private webhook receivers require explicit administrator authorization and a private-only DNS result")
    if parsed.scheme != "https" and not (allow_private and all(private_kinds)):
        raise ValueError("Public webhook destinations must use HTTPS")
    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host + (f":{port}" if port else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))


def signed_headers(secret: str, event_id: str, event_type: str, body: bytes) -> dict[str, str]:
    """The NVR webhook signature: HMAC-SHA256 over "<timestamp>.<body>" with the receiver's secret."""
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-NVR-Event-ID": event_id,
        "X-NVR-Event-Type": event_type,
        "X-NVR-Timestamp": timestamp,
        "X-NVR-Signature": f"sha256={signature}",
    }


def url_origin(ciphertext: bytes) -> str:
    parsed = urlsplit(decrypt(ciphertext))
    host = parsed.hostname or ""
    host = f"[{host}]" if ":" in host else host
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def create_endpoint(name: str, target_url: str, events: list[str], allow_private: bool, created_by: int) -> tuple[dict[str, Any], str]:
    if not name.strip() or len(name.strip()) > 100:
        raise ValueError("Webhook name must be between 1 and 100 characters")
    if not events or len(events) != len(set(events)) or any(event not in EVENT_TYPES - {"webhook.test"} for event in events):
        raise ValueError("Select one or more supported incident event types")
    target = validate_target_url(target_url, allow_private)
    secret = secrets.token_urlsafe(32)
    endpoint_id = str(uuid.uuid4())
    now = _now()
    with connect() as db:
        db.execute(
            "INSERT INTO webhook_endpoints(id,name,target_url_ciphertext,signing_secret_ciphertext,events_json,allow_private,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (endpoint_id, name.strip(), encrypt(target), encrypt(secret), json.dumps(sorted(events)), int(allow_private), created_by, now, now),
        )
        row = db.execute("SELECT * FROM webhook_endpoints WHERE id=?", (endpoint_id,)).fetchone()
        return _endpoint_view(row), secret


def _endpoint_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "url_origin": url_origin(row["target_url_ciphertext"]),
        "events": json.loads(row["events_json"]), "allow_private_receiver": bool(row["allow_private"]),
        "enabled": bool(row["enabled"]), "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def list_endpoints() -> list[dict[str, Any]]:
    with connect() as db:
        return [_endpoint_view(row) for row in db.execute("SELECT * FROM webhook_endpoints ORDER BY name COLLATE NOCASE").fetchall()]


def update_endpoint(endpoint_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM webhook_endpoints WHERE id=?", (endpoint_id,)).fetchone()
        if row is None:
            return None
        changes: list[str] = []
        values: list[Any] = []
        if "name" in fields:
            name = str(fields["name"]).strip()
            if not name or len(name) > 100:
                raise ValueError("Webhook name must be between 1 and 100 characters")
            changes.append("name=?"); values.append(name)
        allow_private = bool(fields.get("allow_private_receiver", row["allow_private"]))
        if "url" in fields:
            changes.append("target_url_ciphertext=?"); values.append(encrypt(validate_target_url(str(fields["url"]), allow_private)))
        if "events" in fields:
            events = fields["events"]
            if not events or len(events) != len(set(events)) or any(event not in EVENT_TYPES - {"webhook.test"} for event in events):
                raise ValueError("Select one or more supported incident event types")
            changes.append("events_json=?"); values.append(json.dumps(sorted(events)))
        if "allow_private_receiver" in fields:
            if "url" not in fields:
                validate_target_url(decrypt(row["target_url_ciphertext"]), allow_private)
            changes.append("allow_private=?"); values.append(int(allow_private))
        if "enabled" in fields:
            changes.append("enabled=?"); values.append(int(bool(fields["enabled"])))
        if changes:
            changes.append("updated_at=?"); values.append(_now()); values.append(endpoint_id)
            db.execute(f"UPDATE webhook_endpoints SET {','.join(changes)} WHERE id=?", values)
        return _endpoint_view(db.execute("SELECT * FROM webhook_endpoints WHERE id=?", (endpoint_id,)).fetchone())


def rotate_secret(endpoint_id: str) -> str | None:
    secret = secrets.token_urlsafe(32)
    with connect() as db:
        cursor = db.execute("UPDATE webhook_endpoints SET signing_secret_ciphertext=?,updated_at=? WHERE id=?", (encrypt(secret), _now(), endpoint_id))
        return secret if cursor.rowcount else None


def delete_endpoint(endpoint_id: str) -> bool:
    with connect() as db:
        db.execute("UPDATE webhook_deliveries SET status='failed',error_code='endpoint_deleted',updated_at=? WHERE endpoint_id=? AND status IN ('queued','delivering')", (_now(), endpoint_id))
        cursor = db.execute("DELETE FROM webhook_endpoints WHERE id=?", (endpoint_id,))
        return cursor.rowcount > 0


def enqueue_event(db: sqlite3.Connection, event_type: str, payload: dict[str, Any], endpoint_id: str | None = None) -> int:
    if event_type not in EVENT_TYPES:
        return 0
    if endpoint_id is None:
        endpoints = db.execute("SELECT id,name,events_json FROM webhook_endpoints WHERE enabled=1").fetchall()
        recipients = [(row["id"], row["name"]) for row in endpoints if event_type in json.loads(row["events_json"])]
    else:
        endpoint = db.execute("SELECT id,name FROM webhook_endpoints WHERE id=? AND enabled=1", (endpoint_id,)).fetchone()
        recipients = [(endpoint["id"], endpoint["name"])] if endpoint else []
    created = 0
    now = _now()
    for target_id, target_name in recipients:
        pending = db.execute("SELECT COUNT(*) FROM webhook_deliveries WHERE endpoint_id=? AND status IN ('queued','delivering')", (target_id,)).fetchone()[0]
        delivery_id = str(uuid.uuid4())
        status = "failed" if pending >= MAX_PENDING_PER_ENDPOINT else "queued"
        error = "queue_capacity" if status == "failed" else None
        cursor = db.execute(
            "INSERT OR IGNORE INTO webhook_deliveries(id,endpoint_id,endpoint_name,event_id,event_type,payload_json,status,attempt_count,next_attempt_at,error_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,0,?,?,?,?)",
            (delivery_id, target_id, target_name, str(payload["event_id"]), event_type, json.dumps(payload, sort_keys=True, separators=(",", ":")), status, now, error, now, now),
        )
        created += cursor.rowcount
    return created


def enqueue_test(endpoint_id: str, actor_id: int) -> str | None:
    event_id = str(uuid.uuid4())
    with connect() as db:
        if db.execute("SELECT 1 FROM webhook_endpoints WHERE id=?", (endpoint_id,)).fetchone() is None:
            return None
        payload = {"event_id": event_id, "event_type": "webhook.test", "occurred_at": _now(), "test": True, "requested_by": int(actor_id)}
        if enqueue_event(db, "webhook.test", payload, endpoint_id) == 0:
            return None
        return event_id


def list_deliveries(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT d.* FROM webhook_deliveries d ORDER BY d.created_at DESC LIMIT ?",
            (min(max(int(limit), 1), 500),),
        ).fetchall()
    return [{key: row[key] for key in ("id", "endpoint_id", "endpoint_name", "event_id", "event_type", "status", "attempt_count", "next_attempt_at", "response_status", "error_code", "created_at", "updated_at", "delivered_at")} for row in rows]


def recover_inflight() -> None:
    with connect() as db:
        db.execute("UPDATE webhook_deliveries SET status='queued',next_attempt_at=?,updated_at=?,error_code='process_restarted' WHERE status='delivering'", (_now(), _now()))


def claim_due_delivery() -> dict[str, Any] | None:
    now = _now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT d.*,e.target_url_ciphertext,e.signing_secret_ciphertext,e.allow_private,e.enabled AS endpoint_enabled "
            "FROM webhook_deliveries d JOIN webhook_endpoints e ON e.id=d.endpoint_id "
            "WHERE d.status='queued' AND d.next_attempt_at<=? ORDER BY d.created_at LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            return None
        attempt = int(row["attempt_count"]) + 1
        db.execute("UPDATE webhook_deliveries SET status='delivering',attempt_count=?,updated_at=? WHERE id=?", (attempt, now, row["id"]))
        return {**dict(row), "attempt_count": attempt}


def finish_delivery(delivery_id: str, attempt: int, *, response_status: int | None = None, error_code: str | None = None, retryable: bool = False) -> None:
    now = datetime.now(timezone.utc)
    if response_status is not None and 200 <= response_status < 300:
        with connect() as db:
            db.execute("UPDATE webhook_deliveries SET status='delivered',response_status=?,error_code=NULL,updated_at=?,delivered_at=? WHERE id=?", (response_status, now.isoformat(), now.isoformat(), delivery_id))
        return
    may_retry = retryable and attempt < MAX_ATTEMPTS
    next_time = (now + timedelta(seconds=RETRY_SECONDS[min(attempt - 1, len(RETRY_SECONDS) - 1)])).isoformat()
    with connect() as db:
        db.execute(
            "UPDATE webhook_deliveries SET status=?,response_status=?,error_code=?,updated_at=?,next_attempt_at=? WHERE id=?",
            ("queued" if may_retry else "failed", response_status, error_code or "delivery_failed", now.isoformat(), next_time, delivery_id),
        )


class WebhookManager:
    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.stopping = False

    async def start(self) -> None:
        self.stopping = False
        recover_inflight()
        self.task = asyncio.create_task(self._run(), name="webhook-delivery-worker")

    async def stop(self) -> None:
        self.stopping = True
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _run(self) -> None:
        while not self.stopping:
            try:
                delivery = await asyncio.to_thread(claim_due_delivery)
                if delivery is None:
                    await asyncio.sleep(1)
                    continue
                await self._deliver(delivery)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)

    async def _finish(self, delivery_id: str, attempt: int, **result: Any) -> None:
        await asyncio.to_thread(finish_delivery, delivery_id, attempt, **result)

    async def _deliver(self, delivery: dict[str, Any]) -> None:
        delivery_id = str(delivery["id"])
        attempt = int(delivery["attempt_count"])
        if not delivery["endpoint_enabled"]:
            await self._finish(delivery_id, attempt, error_code="endpoint_disabled")
            return
        try:
            target = await asyncio.wait_for(
                asyncio.to_thread(validate_target_url, decrypt(delivery["target_url_ciphertext"]), bool(delivery["allow_private"])),
                timeout=5,
            )
            body = json.dumps(json.loads(delivery["payload_json"]), sort_keys=True, separators=(",", ":")).encode("utf-8")
            headers = signed_headers(decrypt(delivery["signing_secret_ciphertext"]), str(delivery["event_id"]), str(delivery["event_type"]), body)
            async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=3), follow_redirects=False, trust_env=False) as client:
                response = await client.post(target, content=body, headers=headers)
            code = int(response.status_code)
            await self._finish(delivery_id, attempt, response_status=code, error_code=None if 200 <= code < 300 else f"receiver_http_{code}", retryable=code == 429 or code >= 500)
        except Exception as exc:
            error_code = "invalid_destination" if isinstance(exc, ValueError) else f"transport_{type(exc).__name__}"
            await self._finish(delivery_id, attempt, error_code=error_code, retryable=not isinstance(exc, ValueError))

