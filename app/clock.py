"""The two time helpers every module used to define for itself (#284).

``now()`` is the current time, timezone-aware, in UTC. ``parse()`` reads a stored timestamp in any of the forms the database holds (``...Z``,
``+00:00``, or none, which counts as UTC) and returns an aware datetime, keeping the offset it was written with.

Modules keep their own ``_now``/``_parse`` names (``from app.clock import now as _now``), so a test that patches one module's clock still does.
``stamp()`` is the one form new timestamps are *written* in: UTC, millisecond precision, ``Z`` (``2026-11-12T09:30:00.123Z``), the same form SQLite's
``strftime('%Y-%m-%dT%H:%M:%fZ','now')`` column defaults produce. Fixed-width, so comparing two of them as text orders them in time. Paths move to it one at a time
(sessions, API tokens, ...); a column's readers and its comparisons move together, because a text comparison between two forms can misorder two instants less than
a second apart (``...:00Z`` sorts after ``...:00.5+00:00``).
"""
from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def stamp(value: datetime | None = None) -> str:
    """``value`` (default: now) as the canonical stored form: UTC, milliseconds, ``Z``. A naive time counts as UTC."""
    moment = now() if value is None else value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.astimezone(timezone.utc).microsecond // 1000:03d}Z"
