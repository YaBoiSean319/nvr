"""Worker liveness records shared by the supervisor, workers and status API."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from app.store import connect

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 5
LOOP_LAG_WARN_SECONDS = 5.0
LOOP_TICK_SECONDS = 1.0
STALE_AFTER = timedelta(seconds=30)
SUPERVISED_WORKERS = ("web", "recorder", "exporter", "ai", "rules", "background")


def record(name: str, *, state: str, pid: int | None = None, started: bool = False,
           exit_code: int | None = None, exited: bool = False, restarted: bool = False) -> None:
    now = _now()
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO worker_status(name,state) VALUES(?,?)", (name, state))
        db.execute(
            "UPDATE worker_status SET state=?,pid=COALESCE(?,pid),"
            "started_at=CASE WHEN ? THEN ? ELSE started_at END,"
            "heartbeat_at=CASE WHEN ? THEN NULL ELSE heartbeat_at END,"
            "last_exit_at=CASE WHEN ? THEN ? ELSE last_exit_at END,"
            "last_exit_code=CASE WHEN ? THEN ? ELSE last_exit_code END,"
            "restarts=restarts+?,updated_at=? WHERE name=?",
            (state, pid, started, now, started, exited, now, exited, exit_code, int(restarted), now, name),
        )


def beat(name: str) -> None:
    with connect() as db:
        db.execute(
            "UPDATE worker_status SET state='running',pid=?,heartbeat_at=?,updated_at=? WHERE name=?",
            (os.getpid(), _now(), _now(), name),
        )


async def heartbeat_forever(name: str) -> None:
    """Report this process alive until cancelled."""
    while True:
        try:
            await asyncio.to_thread(beat, name)
        except sqlite3.Error:
            pass
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def watch_loop_lag(name: str, warn_after: float = LOOP_LAG_WARN_SECONDS, tick: float = LOOP_TICK_SECONDS) -> None:
    """Say so in the log when this process's event loop was blocked, since a loop blocked for 30 s stops the heartbeat and gets the worker killed."""
    loop = asyncio.get_running_loop()
    expected = loop.time() + tick
    while True:
        await asyncio.sleep(tick)
        late = loop.time() - expected
        if late >= warn_after:
            logger.warning("%s worker: the event loop was blocked for %.1f s (the supervisor restarts a worker whose loop is blocked for %.0f s)", name, late, STALE_AFTER.total_seconds())
        expected = loop.time() + tick


def is_fresh(row: sqlite3.Row, now: datetime | None = None) -> bool:
    if row["state"] != "running" or not row["heartbeat_at"]:
        return False
    return (now or datetime.now(timezone.utc)) - _parse(row["heartbeat_at"]) <= STALE_AFTER


def report(supervised: bool) -> list[dict[str, object]]:
    if not supervised:
        return [{"name": name, "mode": "in_process", "alive": True} for name in SUPERVISED_WORKERS]
    with connect() as db:
        rows = {row["name"]: row for row in db.execute("SELECT * FROM worker_status")}
    workers = []
    for name in SUPERVISED_WORKERS:
        row = rows.get(name)
        workers.append({
            "name": name,
            "mode": "process",
            "alive": bool(row) and is_fresh(row),
            "state": row["state"] if row else "unknown",
            "pid": row["pid"] if row else None,
            "started_at": row["started_at"] if row else None,
            "heartbeat_at": row["heartbeat_at"] if row else None,
            "restarts": row["restarts"] if row else 0,
            "last_exit_at": row["last_exit_at"] if row else None,
            "last_exit_code": row["last_exit_code"] if row else None,
        })
    return workers


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
