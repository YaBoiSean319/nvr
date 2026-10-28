"""Sign-in rate limiting, per account, per account-and-address pair, and per address.

Every attempt is counted before it is judged, in the same transaction that checks whether it
may go ahead, so parallel guesses cannot all slip past a check made before any was counted.
A success undoes its own count. After a few failures every further attempt waits, twice as
long each time, up to a cap: a guessing attacker is slowed to a handful of tries an hour, and
no one is ever locked out for longer than the cap.

Three buckets are kept. The pair (this account from this address) has a low threshold, so an
attacker is stopped quickly yet cannot keep the real person, signing in from elsewhere, waiting.
The account on its own has a high threshold, which spreading guesses over many addresses
reaches. The address has a high threshold too, for one machine trying many accounts. Failures
are forgotten after a quiet spell, and a completed sign-in clears the account's buckets. A
throttled attempt is refused even with the right credentials, or the throttle would not slow
guessing.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from . import network
from .audit import record
from .store import connect

# (failures allowed before waiting starts, first wait in seconds)
LIMITS = {"pair": (4, 2), "account": (19, 2), "address": (19, 2)}
MAX_WAIT_SECONDS = 300
FORGET_AFTER = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def client_address(request: Request) -> str:
    """The caller's address: the peer's, or behind a trusted proxy (``NVR_TRUSTED_PROXIES``) the address it reports (see ``network``)."""
    return network.client_address(request)


def account_key(username: str) -> str:
    return username.strip().casefold()[:128]


def _buckets(account: str, address: str, include_address: bool = True) -> list[tuple[str, str]]:
    buckets = []
    if account:
        buckets += [("pair", f"{account}|{address}"), ("account", account)]
    if include_address and address:
        buckets.append(("address", address))
    return buckets


def _wait(scope: str, failures: int) -> int:
    allowed, first = LIMITS[scope]
    if failures <= allowed:
        return 0
    return min(MAX_WAIT_SECONDS, first * 2 ** (failures - allowed - 1))


@dataclass(frozen=True)
class Throttle:
    scope: str
    retry_after: int


def _blocked(db: sqlite3.Connection, buckets: list[tuple[str, str]], now: datetime) -> Throttle | None:
    longest: Throttle | None = None
    for scope, key in buckets:
        row = db.execute("SELECT blocked_until FROM auth_failures WHERE scope=? AND key=?", (scope, key)).fetchone()
        if row and row["blocked_until"] and _parse(row["blocked_until"]) > now:
            wait = int((_parse(row["blocked_until"]) - now).total_seconds()) + 1
            if longest is None or wait > longest.retry_after:
                longest = Throttle(scope, wait)
    return longest


def _count(db: sqlite3.Connection, buckets: list[tuple[str, str]], address: str, now: datetime) -> None:
    db.execute("DELETE FROM auth_failures WHERE last_failure_at<? AND (blocked_until IS NULL OR blocked_until<?)",
               ((now - FORGET_AFTER).isoformat(), now.isoformat()))
    for scope, key in buckets:
        row = db.execute("SELECT failures,last_failure_at FROM auth_failures WHERE scope=? AND key=?", (scope, key)).fetchone()
        failures = 1 if row is None or now - _parse(row["last_failure_at"]) > FORGET_AFTER else int(row["failures"]) + 1
        wait = _wait(scope, failures)
        db.execute("INSERT INTO auth_failures(scope,key,failures,last_failure_at,blocked_until) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(scope,key) DO UPDATE SET failures=excluded.failures,last_failure_at=excluded.last_failure_at,blocked_until=excluded.blocked_until",
                   (scope, key, failures, now.isoformat(), (now + timedelta(seconds=wait)).isoformat() if wait else None))
        if wait and failures - 1 == LIMITS[scope][0]:
            record(db, None, "auth_throttled", scope, key, failures=failures, wait_seconds=wait, address=address)


def _attempt(account: str, address: str, include_address: bool) -> None:
    now = _now()
    buckets = _buckets(account, address, include_address)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            throttle = _blocked(db, buckets, now)
            if throttle is None:
                _count(db, buckets, address, now)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    if throttle:
        raise HTTPException(status_code=429, detail=f"Too many failed attempts. Try again in {throttle.retry_after} seconds.",
                            headers={"Retry-After": str(throttle.retry_after)})


def begin(account: str, address: str) -> None:
    """Count an attempt, or refuse it (429, with Retry-After) while the account, the pair or the address is waiting.

    The attempt counts as a failure until ``release`` or ``succeeded`` says otherwise. ``account`` may be empty
    when it is not yet known (a passkey on its own): only the address is counted."""
    _attempt(account, address, include_address=True)


def begin_account(account: str, address: str) -> None:
    """Count an attempt against an account that has just been identified, not the address (already counted by ``begin``)."""
    _attempt(account, address, include_address=False)


def record_failure(account: str, address: str) -> None:
    """Count a failure without judging it (for callers that already know it failed)."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _count(db, _buckets(account, address), address, _now())
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def release(account: str, address: str, include_address: bool = True) -> None:
    """Take back an attempt's count because it was right, leaving earlier failures in place."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for scope, key in _buckets(account, address, include_address):
                row = db.execute("SELECT failures,last_failure_at FROM auth_failures WHERE scope=? AND key=?", (scope, key)).fetchone()
                if row is None:
                    continue
                failures = max(0, int(row["failures"]) - 1)
                if failures == 0:
                    db.execute("DELETE FROM auth_failures WHERE scope=? AND key=?", (scope, key))
                    continue
                wait = _wait(scope, failures)
                until = (_parse(row["last_failure_at"]) + timedelta(seconds=wait)).isoformat() if wait else None
                db.execute("UPDATE auth_failures SET failures=?,blocked_until=? WHERE scope=? AND key=?", (failures, until, scope, key))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def succeeded(account: str, address: str) -> None:
    """A completed sign-in: this attempt's count goes, and the account's failures with it."""
    release(account, address)
    with connect() as db:
        db.execute("DELETE FROM auth_failures WHERE (scope='account' AND key=?) OR (scope='pair' AND key=?)", (account, f"{account}|{address}"))
