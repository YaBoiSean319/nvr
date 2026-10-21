from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .credentials import decrypt, encrypt
from .store import connect
from .webhooks import validate_target_url, url_origin

MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 900
REQUEST_TIMEOUT_SECONDS = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def heartbeat_status() -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM heartbeat_configuration WHERE singleton_id=1").fetchone()
    if row is None:
        return {
            "configured": False, "enabled": False, "url_origin": None, "interval_seconds": None,
            "instance_id": None, "last_attempt_at": None, "last_success_at": None,
            "last_http_status": None, "last_error": None,
        }
    return {
        "configured": bool(row["target_url_ciphertext"]), "enabled": bool(row["enabled"]),
        "url_origin": url_origin(row["target_url_ciphertext"]) if row["target_url_ciphertext"] else None,
        "allow_private_receiver": bool(row["allow_private"]), "interval_seconds": int(row["interval_seconds"]),
        "instance_id": row["instance_id"], "last_attempt_at": row["last_attempt_at"],
        "last_success_at": row["last_success_at"], "last_http_status": row["last_http_status"],
        "last_error": row["last_error"],
        "checker_note": "Last success means this NVR received a successful HTTP response. It does not prove an independent off-host checker is configured or alerting.",
    }


def configure_heartbeat(target_url: str, interval_seconds: int, allow_private: bool, actor_id: int) -> tuple[dict[str, Any], str | None]:
    if not MIN_INTERVAL_SECONDS <= interval_seconds <= MAX_INTERVAL_SECONDS:
        raise ValueError(f"Heartbeat interval must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS} seconds")
    target = validate_target_url(target_url, allow_private)
    now = _now()
    with connect() as db:
        current = db.execute("SELECT signing_secret_ciphertext,instance_id,created_at FROM heartbeat_configuration WHERE singleton_id=1").fetchone()
        secret = None if current is not None else secrets.token_urlsafe(32)
        instance_id = str(current["instance_id"]) if current else str(uuid.uuid4())
        created_at = str(current["created_at"]) if current else now
        db.execute(
            "INSERT INTO heartbeat_configuration(singleton_id,target_url_ciphertext,signing_secret_ciphertext,allow_private,interval_seconds,enabled,instance_id,force_next_test,next_attempt_at,last_attempt_at,last_success_at,last_http_status,last_error,created_by,created_at,updated_at) "
            "VALUES(1,?,?,?,?,1,?,0,?,NULL,NULL,NULL,NULL,?,?,?) ON CONFLICT(singleton_id) DO UPDATE SET target_url_ciphertext=excluded.target_url_ciphertext,allow_private=excluded.allow_private,interval_seconds=excluded.interval_seconds,enabled=1,force_next_test=0,next_attempt_at=excluded.next_attempt_at,last_attempt_at=NULL,last_success_at=NULL,last_http_status=NULL,last_error=NULL,created_by=excluded.created_by,updated_at=excluded.updated_at",
            (encrypt(target), encrypt(secret) if secret else current["signing_secret_ciphertext"], int(allow_private), interval_seconds, instance_id, now, actor_id, created_at, now),
        )
    return heartbeat_status(), secret


def rotate_heartbeat_secret() -> str | None:
    secret = secrets.token_urlsafe(32)
    with connect() as db:
        cursor = db.execute(
            "UPDATE heartbeat_configuration SET signing_secret_ciphertext=?,next_attempt_at=?,updated_at=? WHERE singleton_id=1 AND enabled=1",
            (encrypt(secret), _now(), _now()),
        )
        return secret if cursor.rowcount else None


def disable_heartbeat() -> bool:
    with connect() as db:
        cursor = db.execute("UPDATE heartbeat_configuration SET enabled=0,force_next_test=0,updated_at=? WHERE singleton_id=1", (_now(),))
        return cursor.rowcount > 0


def queue_heartbeat_test() -> bool:
    with connect() as db:
        cursor = db.execute(
            "UPDATE heartbeat_configuration SET force_next_test=1,next_attempt_at=?,updated_at=? WHERE singleton_id=1 AND enabled=1",
            (_now(), _now()),
        )
        return cursor.rowcount > 0


def claim_due_heartbeat() -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM heartbeat_configuration WHERE singleton_id=1 AND enabled=1 AND next_attempt_at<=?", (now_text,)).fetchone()
        if row is None:
            return None
        test = bool(row["force_next_test"])
        next_attempt = (now + timedelta(seconds=int(row["interval_seconds"]))).isoformat()
        db.execute(
            "UPDATE heartbeat_configuration SET force_next_test=0,next_attempt_at=?,last_attempt_at=?,last_error=NULL,updated_at=? WHERE singleton_id=1",
            (next_attempt, now_text, now_text),
        )
        return {**dict(row), "test": test}


def record_heartbeat_result(*, success: bool, http_status: int | None = None, error: str | None = None) -> None:
    now = _now()
    with connect() as db:
        if success:
            db.execute(
                "UPDATE heartbeat_configuration SET last_success_at=?,last_http_status=?,last_error=NULL,updated_at=? WHERE singleton_id=1",
                (now, http_status, now),
            )
        else:
            db.execute(
                "UPDATE heartbeat_configuration SET last_http_status=?,last_error=?,updated_at=? WHERE singleton_id=1",
                (http_status, error or "send_failed", now),
            )


class HeartbeatManager:
    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.stopping = False

    async def start(self) -> None:
        self.stopping = False
        self.task = asyncio.create_task(self._run(), name="external-heartbeat-worker")

    async def stop(self) -> None:
        self.stopping = True
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _run(self) -> None:
        while not self.stopping:
            try:
                config = await asyncio.to_thread(claim_due_heartbeat)
                if config is None:
                    await asyncio.sleep(1)
                    continue
                await self._send(config)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.to_thread(record_heartbeat_result, success=False, error="worker_error")
                await asyncio.sleep(1)

    async def _send(self, config: dict[str, Any]) -> None:
        try:
            target = await asyncio.wait_for(
                asyncio.to_thread(validate_target_url, decrypt(config["target_url_ciphertext"]), bool(config["allow_private"])),
                timeout=5,
            )
            secret = decrypt(config["signing_secret_ciphertext"]).encode("utf-8")
            sent_at = str(int(time.time()))
            event_id = str(uuid.uuid4())
            body = json.dumps({
                "event_id": event_id,
                "event_type": "nvr.heartbeat",
                "instance_id": config["instance_id"],
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "test": bool(config["test"]),
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")
            signature = hmac.new(secret, sent_at.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
            headers = {
                "Content-Type": "application/json", "X-NVR-Event-ID": event_id,
                "X-NVR-Event-Type": "nvr.heartbeat", "X-NVR-Timestamp": sent_at,
                "X-NVR-Signature": f"sha256={signature}",
            }
            async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=3), follow_redirects=False, trust_env=False) as client:
                response = await client.post(target, content=body, headers=headers)
            status_code = int(response.status_code)
            await asyncio.to_thread(record_heartbeat_result, success=200 <= status_code < 300, http_status=status_code, error=None if 200 <= status_code < 300 else f"receiver_http_{status_code}")
        except asyncio.CancelledError:
            raise
        except ValueError:
            await asyncio.to_thread(record_heartbeat_result, success=False, error="invalid_destination")
        except Exception as exc:
            await asyncio.to_thread(record_heartbeat_result, success=False, error=f"transport_{type(exc).__name__}")

