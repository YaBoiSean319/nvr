"""Emails about the NVR itself, and the summary of alert emails held back (#231).

Problems the NVR finds in itself (a camera that stopped recording, a full disk, the AI worker stopping, a covered camera...) can be emailed to chosen people through the same
Microsoft 365 connection as rule alerts. They go through a small queue with retries; a limit of an hour keeps a flapping fault from sending hundreds, and the alert emails that any
limit (this one, or a rule's) held back are counted and told about in one summary email. A fault during a maintenance window is not emailed: incidents hold their notifications
until it ends (``maintenance``), before they get here.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import ms_mail
from .store import connect

INCIDENT_EMAILS_PER_HOUR = 30
SUMMARY_AFTER = timedelta(minutes=10)  # Quiet this long after the last held email, then one summary
MAX_ATTEMPTS = 8
RETRY_SECONDS = (2, 5, 15, 60, 300, 1800, 3600)
KEEP = timedelta(days=30)

TITLES = {
    "recording": "Recording problem", "recording_uncertain": "Recording may have a gap", "storage": "Storage problem", "disk_health": "Disk health warning",
    "retention_forecast": "Footage will not last as long as the policy says", "tiering": "Storage tiering problem", "backup": "Backup problem", "self_test": "Self-test problem",
    "failover": "Failover problem", "failover_sync": "Failover sync problem",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def settings(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute("SELECT enabled,recipients_json FROM incident_emails WHERE singleton_id=1").fetchone()
    return {"enabled": bool(row["enabled"]), "recipients": json.loads(row["recipients_json"])} if row else {"enabled": False, "recipients": []}


def save_settings(db: sqlite3.Connection, enabled: bool, recipients: list[str]) -> dict[str, Any]:
    clean = list(dict.fromkeys(ms_mail.valid_address(item).lower() for item in recipients))
    if len(clean) > ms_mail.MAX_RECIPIENTS:
        raise ValueError(f"At most {ms_mail.MAX_RECIPIENTS} addresses")
    if enabled and not clean:
        raise ValueError("Enter at least one address to email problems to")
    db.execute("INSERT INTO incident_emails(singleton_id,enabled,recipients_json,updated_at) VALUES(1,?,?,?) ON CONFLICT(singleton_id) DO UPDATE SET enabled=excluded.enabled,"
               "recipients_json=excluded.recipients_json,updated_at=excluded.updated_at", (int(enabled), json.dumps(clean), _iso(_now())))
    return settings(db)


def held(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute("SELECT held,first_held_at,last_held_at FROM email_held WHERE singleton_id=1").fetchone()
    return {"count": row["held"], "since": row["first_held_at"]} if row and row["held"] else {"count": 0, "since": None}


def note_held(db: sqlite3.Connection) -> None:
    """One alert email was not sent because a limit was reached."""
    stamp = _iso(_now())
    db.execute("INSERT INTO email_held(singleton_id,held,first_held_at,last_held_at) VALUES(1,1,?,?) ON CONFLICT(singleton_id) DO UPDATE SET held=held+1,"
               "first_held_at=COALESCE(CASE WHEN held>0 THEN first_held_at END,excluded.first_held_at),last_held_at=excluded.last_held_at", (stamp, stamp))


def _enqueue(db: sqlite3.Connection, kind: str, subject: str, body: str, recipients: list[str]) -> None:
    stamp = _iso(_now())
    db.execute("INSERT INTO email_outbox(id,kind,subject,body,recipients_json,next_attempt_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
               (str(uuid.uuid4()), kind, subject[:ms_mail.MAX_SUBJECT], body[:ms_mail.MAX_BODY], json.dumps(recipients), stamp, stamp, stamp))


def queue_incident(db: sqlite3.Connection, incident: sqlite3.Row, event_type: str, occurred_at: str, details: dict[str, Any]) -> bool:
    """Email a raised, recurred or recovered incident to the chosen people, inside the caller's transaction. False when nobody is set to be told or the hourly limit held it back."""
    chosen = settings(db)
    if not chosen["enabled"] or not chosen["recipients"] or event_type not in {"raised", "recurred", "recovered"}:
        return False
    since = _iso(_now() - timedelta(hours=1))
    if db.execute("SELECT COUNT(*) FROM email_outbox WHERE kind='incident' AND created_at>?", (since,)).fetchone()[0] >= INCIDENT_EMAILS_PER_HOUR:
        note_held(db)
        return False
    camera = db.execute("SELECT name FROM cameras WHERE id=?", (incident["camera_id"],)).fetchone() if incident["camera_id"] else None
    title = TITLES.get(str(incident["fault_type"]), "Camera tamper" if str(incident["fault_type"]).startswith("tamper_") else "AI analysis problem" if str(incident["fault_type"]).startswith("ai_")
                       else "System problem" if str(incident["fault_type"]).startswith("worker_") else "Problem")
    recovered = event_type == "recovered"
    subject = f"[NVR] {'Recovered: ' if recovered else ''}{title}: {incident['summary']}"
    public = os.environ.get("NVR_PUBLIC_URL", "").strip().rstrip("/")
    lines = [incident["summary"], f"{'Recovered' if recovered else 'Problem'}: {title}"]
    if camera is not None:
        lines.append(f"Camera: {camera['name']}")
    lines.append(f"At {occurred_at[:19].replace('T', ' ')} UTC")
    if event_type == "recurred" or int(incident["recurrence_count"] or 1) > 1:
        lines.append(f"This has happened {int(incident['recurrence_count'])} times")
    if details.get("held_during_maintenance"):
        lines.append("It started during a maintenance window and is still there now that the window has ended")
    if public:
        lines += ["", f"Open the NVR (sign in required): {public}"]
    lines += ["", "No footage or camera addresses are included in this message."]
    _enqueue(db, "incident", subject, "\n".join(lines), chosen["recipients"])
    return True


def queue_held_summary(now: datetime | None = None) -> bool:
    """When alert emails were held back by a limit and things have gone quiet, queue one summary of how many (to the people told about problems; the Email page shows the count whatever)."""
    now = now or _now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT held,first_held_at,last_held_at FROM email_held WHERE singleton_id=1").fetchone()
            chosen = settings(db)
            if row is None or not row["held"] or not chosen["recipients"] or datetime.fromisoformat(row["last_held_at"]) > now - SUMMARY_AFTER:
                db.execute("COMMIT")
                return False
            _enqueue(db, "held_summary", f"[NVR] {row['held']} alert email{'s' if row['held'] != 1 else ''} held back",
                     f"{row['held']} alert email{'s were' if row['held'] != 1 else ' was'} not sent since {row['first_held_at'][:19].replace('T', ' ')} UTC because the limit on emails an hour "
                     f"was reached. The alerts themselves are in the NVR (the alarm inbox and the incident list).\n\nNo footage or camera addresses are included in this message.", chosen["recipients"])
            db.execute("UPDATE email_held SET held=0,first_held_at=NULL,last_held_at=NULL WHERE singleton_id=1")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return True


def _claim() -> sqlite3.Row | None:
    stamp = _iso(_now())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM email_outbox WHERE status='queued' AND next_attempt_at<=? ORDER BY next_attempt_at,created_at LIMIT 1", (stamp,)).fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            db.execute("UPDATE email_outbox SET status='sending',attempt_count=attempt_count+1,updated_at=? WHERE id=?", (stamp, row["id"]))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return row


def recover_inflight() -> None:
    with connect() as db:
        db.execute("UPDATE email_outbox SET status='queued',next_attempt_at=?,last_error='process_restarted' WHERE status='sending'", (_iso(_now()),))


def deliver_one() -> bool:
    """Send the next due email. Returns False when there was none."""
    queue_held_summary()
    row = _claim()
    if row is None:
        return False
    attempt = int(row["attempt_count"]) + 1
    now = _now()
    try:
        ms_mail.send(json.loads(row["recipients_json"]), row["subject"], row["body"])
    except ms_mail.MailError as exc:
        ms_mail.note_failure(exc)
        retry = exc.retryable and attempt < MAX_ATTEMPTS
        with connect() as db:
            db.execute("UPDATE email_outbox SET status=?,last_error=?,next_attempt_at=?,updated_at=? WHERE id=?",
                       ("queued" if retry else "failed", f"email_{exc.code}", _iso(now + timedelta(seconds=RETRY_SECONDS[min(attempt - 1, len(RETRY_SECONDS) - 1)])), _iso(now), row["id"]))
        return True
    ms_mail.note_success()
    with connect() as db:
        db.execute("UPDATE email_outbox SET status='sent',last_error=NULL,sent_at=?,updated_at=? WHERE id=?", (_iso(now), _iso(now), row["id"]))
        db.execute("DELETE FROM email_outbox WHERE status IN ('sent','failed') AND updated_at<?", (_iso(now - KEEP),))
    return True


def recent(db: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    return [{key: row[key] for key in ("kind", "subject", "status", "attempt_count", "last_error", "created_at", "sent_at")}
            for row in db.execute("SELECT * FROM email_outbox ORDER BY created_at DESC,id LIMIT ?", (limit,))]
