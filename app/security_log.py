"""Reading and pruning the security log (#367).

``app.audit.record`` writes a row for nearly every sensitive action: sign-in throttling, second factors, tokens, grants, settings. This module
is how an administrator reads them (newest first, by date, person and action) and how old rows leave. Rows are written with SQLite's own clock
in the same form as ``app.clock.stamp``, so text comparison orders them by time."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .clock import now as _now, stamp
from .store import has_row

RETENTION_DAYS = (30, 3650)


def retention_days(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT security_log_retention_days FROM security_settings WHERE singleton_id=1").fetchone()[0])


def prune(db: sqlite3.Connection, now: datetime | None = None) -> int:
    """Remove rows older than the retention period and return how many. It reads first: a pass with nothing to remove, which is nearly every
    pass, never takes the database's write lock (#280)."""
    cutoff = stamp((now or _now()) - timedelta(days=retention_days(db)))
    if not has_row(db, "SELECT 1 FROM audit_log WHERE created_at<? LIMIT 1", (cutoff,)):
        return 0
    return db.execute("DELETE FROM audit_log WHERE created_at<?", (cutoff,)).rowcount


def unknown_name(typed: str) -> str:
    """What the log keeps of a username that belongs to nobody. People type passwords into the username box, so the text itself is never
    stored: a keyed digest still shows that the same thing was tried repeatedly, and cannot be reversed or looked up in a table."""
    from .credentials import derived_key

    return "unknown:" + hmac.new(derived_key("security-log-username"), typed.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:16]


def actions(db: sqlite3.Connection) -> list[str]:
    """Every action the log holds, for the filter. Stepping from one action to the next through the index reads a row per action, not per entry."""
    rows = db.execute("WITH RECURSIVE a(x) AS (SELECT MIN(action) FROM audit_log UNION ALL SELECT (SELECT MIN(action) FROM audit_log WHERE action>a.x) FROM a WHERE a.x IS NOT NULL) "
                      "SELECT x FROM a WHERE x IS NOT NULL")
    return [row[0] for row in rows]


def _where(start: datetime, end: datetime, actor_id: int | None, action: str | None) -> tuple[str, list[Any]]:
    clauses, values = ["l.created_at>=?", "l.created_at<?"], [stamp(start), stamp(end)]
    if actor_id is not None:
        clauses.append("l.actor_id=?")
        values.append(actor_id)
    if action:
        clauses.append("l.action=?")
        values.append(action)
    return " AND ".join(clauses), values


def entries(db: sqlite3.Connection, start: datetime, end: datetime, actor_id: int | None = None, action: str | None = None,
            limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """The log's rows in a range, newest first, with who did each one. ``actor`` is None for what the NVR did itself or what nobody signed
    in did (a throttled sign-in, a host command)."""
    where, values = _where(start, end, actor_id, action)
    total = db.execute(f"SELECT COUNT(*) FROM audit_log l WHERE {where}", values).fetchone()[0]
    rows = db.execute(f"SELECT l.id,l.created_at,l.actor_id,u.username AS actor,l.action,l.resource_type,l.resource_id,l.details_json FROM audit_log l "
                      f"LEFT JOIN users u ON u.id=l.actor_id WHERE {where} ORDER BY l.created_at DESC,l.id DESC LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
    return {"total": total, "entries": [{"id": row["id"], "at": row["created_at"], "actor_id": row["actor_id"], "actor": row["actor"], "action": row["action"],
                                         "resource_type": row["resource_type"], "resource_id": row["resource_id"], "details": _details(row["details_json"])} for row in rows]}


def _details(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}
