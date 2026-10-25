"""Sign-in rate limiting, per account and per address.

Each failed attempt counts against both the account named and the address it came from.
After a few failures every further attempt waits, twice as long each time, up to a cap: a
guessing attacker is slowed to a handful of tries an hour, while the account (above all the
emergency administrator) is never locked out for longer than the cap. Failures are forgotten
after a quiet spell, and a completed sign-in clears the account's count. A throttled
attempt is refused even with the right credentials, or the throttle would not slow guessing.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from .audit import record
from .store import connect

# (failures allowed before waiting starts, first wait in seconds)
LIMITS = {"account": (4, 2), "address": (19, 2)}
MAX_WAIT_SECONDS = 300
FORGET_AFTER = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def account_key(username: str) -> str:
    return username.strip().casefold()[:128]


@dataclass(frozen=True)
class Throttle:
    scope: str
    retry_after: int


def _wait(scope: str, failures: int) -> int:
    allowed, first = LIMITS[scope]
    if failures <= allowed:
        return 0
    return min(MAX_WAIT_SECONDS, first * 2 ** (failures - allowed - 1))


def blocked(db: sqlite3.Connection, account: str, address: str) -> Throttle | None:
    now = _now()
    longest: Throttle | None = None
    for scope, key in (("account", account), ("address", address)):
        row = db.execute("SELECT blocked_until FROM auth_failures WHERE scope=? AND key=?", (scope, key)).fetchone()
        if row and row["blocked_until"] and _parse(row["blocked_until"]) > now:
            wait = int((_parse(row["blocked_until"]) - now).total_seconds()) + 1
            if longest is None or wait > longest.retry_after:
                longest = Throttle(scope, wait)
    return longest


def check(account: str, address: str) -> None:
    """Refuse (429, with Retry-After) while this account or address is waiting out failures."""
    with connect() as db:
        throttle = blocked(db, account, address)
    if throttle:
        raise HTTPException(status_code=429, detail=f"Too many failed attempts. Try again in {throttle.retry_after} seconds.",
                            headers={"Retry-After": str(throttle.retry_after)})


def record_failure(account: str, address: str) -> None:
    """Count a failure against the address, and against the account when it names one."""
    now = _now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for scope, key in (("account", account), ("address", address)):
                if not key:
                    continue
                row = db.execute("SELECT failures,last_failure_at FROM auth_failures WHERE scope=? AND key=?", (scope, key)).fetchone()
                failures = 1 if row is None or now - _parse(row["last_failure_at"]) > FORGET_AFTER else int(row["failures"]) + 1
                wait = _wait(scope, failures)
                until = (now + timedelta(seconds=wait)).isoformat() if wait else None
                db.execute("INSERT INTO auth_failures(scope,key,failures,last_failure_at,blocked_until) VALUES(?,?,?,?,?) "
                           "ON CONFLICT(scope,key) DO UPDATE SET failures=excluded.failures,last_failure_at=excluded.last_failure_at,blocked_until=excluded.blocked_until",
                           (scope, key, failures, now.isoformat(), until))
                if wait and failures - 1 == LIMITS[scope][0]:
                    record(db, None, "auth_throttled", scope, key, failures=failures, wait_seconds=wait, **({"address": address} if scope == "account" else {}))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def record_success(account: str) -> None:
    """A completed sign-in clears the account's failures; the address keeps counting until it goes quiet."""
    with connect() as db:
        db.execute("DELETE FROM auth_failures WHERE scope='account' AND key=?", (account,))
