"""The two time helpers every module used to define for itself (#284).

``now()`` is the current time, timezone-aware, in UTC. ``parse()`` reads a stored timestamp in any of the forms the database holds (``...Z``,
``+00:00``, or none, which counts as UTC) and returns an aware datetime, keeping the offset it was written with.

Modules keep their own ``_now``/``_parse`` names (``from app.clock import now as _now``), so a test that patches one module's clock still does.
Which text form new timestamps are *written* in is a separate question and is not decided here.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
