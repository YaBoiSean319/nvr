"""Weekly recording policy evaluation in an explicitly selected IANA timezone."""
from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VALID_MODES = {"motion", "continuous", "disabled"}
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_MINUTES_PER_DAY = 24 * 60
_MINUTES_PER_WEEK = 7 * _MINUTES_PER_DAY


def validate_schedule(timezone_name: str, entries: list[dict[str, object]]) -> list[dict[str, object]]:
    """Validate and normalize weekly intervals. Weekdays use ISO values 0=Mon..6=Sun.

    Intervals are start-inclusive/end-exclusive. An end earlier than a start is
    an overnight interval associated with its start day. Equal endpoints are
    invalid rather than ambiguously meaning either zero or 24 hours.
    """
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Choose a valid IANA timezone, such as America/New_York") from exc
    if len(entries) > 200:
        raise ValueError("A weekly schedule can contain at most 200 intervals")

    occupied: dict[int, str] = {}
    normalized: list[dict[str, object]] = []
    for entry in entries:
        days = entry.get("days")
        start = entry.get("start")
        end = entry.get("end")
        mode = entry.get("mode")
        if not isinstance(days, list) or not days or any(type(day) is not int or day < 0 or day > 6 for day in days):
            raise ValueError("Each interval needs one or more weekdays (0=Monday through 6=Sunday)")
        if len(set(days)) != len(days):
            raise ValueError("Weekdays may not be repeated within an interval")
        if not isinstance(start, str) or not _TIME_RE.fullmatch(start) or not isinstance(end, str) or not _TIME_RE.fullmatch(end):
            raise ValueError("Schedule times must use 24-hour HH:MM format")
        if mode not in VALID_MODES:
            raise ValueError("Schedule mode must be motion, continuous, or disabled")
        start_min = int(start[:2]) * 60 + int(start[3:])
        end_min = int(end[:2]) * 60 + int(end[3:])
        if start_min == end_min:
            raise ValueError("Schedule start and end times must differ")
        for day in days:
            first = day * _MINUTES_PER_DAY + start_min
            last = day * _MINUTES_PER_DAY + end_min
            if end_min < start_min:
                last += _MINUTES_PER_DAY
            for minute in range(first, last):
                slot = minute % _MINUTES_PER_WEEK
                if slot in occupied:
                    raise ValueError("Schedule intervals may not overlap")
                occupied[slot] = str(mode)
        normalized.append({"days": sorted(days), "start": start, "end": end, "mode": mode})
    return normalized


def mode_at(entries: list[dict[str, object]], timezone_name: str, instant: datetime) -> str:
    """Return the scheduled mode at an aware instant, or disabled outside windows.

    Wall-clock schedules are evaluated after converting the UTC instant to the
    configured timezone. During a fall-back repeated hour the interval applies
    to both occurrences. Nonexistent spring-forward minutes do not occur; the
    schedule takes effect at the first real local minute inside its window.
    """
    if instant.tzinfo is None:
        raise ValueError("Schedule evaluation requires a timezone-aware instant")
    local = instant.astimezone(ZoneInfo(timezone_name))
    weekday = local.weekday()
    minute = local.hour * 60 + local.minute
    for entry in entries:
        days = entry["days"]
        start = entry["start"]
        end = entry["end"]
        assert isinstance(days, list) and isinstance(start, str) and isinstance(end, str)
        start_min = int(start[:2]) * 60 + int(start[3:])
        end_min = int(end[:2]) * 60 + int(end[3:])
        if end_min > start_min:
            active = weekday in days and start_min <= minute < end_min
        else:
            previous_day = (weekday - 1) % 7
            active = (weekday in days and minute >= start_min) or (previous_day in days and minute < end_min)
        if active:
            mode = entry["mode"]
            return mode if isinstance(mode, str) else "disabled"
    return "disabled"


def effective_policy(db, camera_id: str, instant: datetime) -> dict[str, object]:
    """Resolve the camera override, inherited group policy, or camera base mode."""
    row = db.execute(
        "SELECT c.enabled,c.recording_enabled,c.recording_mode,c.group_id,"
        "cp.timezone_name AS camera_timezone,cp.schedule_json AS camera_schedule,"
        "gp.timezone_name AS group_timezone,gp.schedule_json AS group_schedule "
        "FROM cameras c LEFT JOIN recording_policy_overrides cp "
        "ON cp.scope_type='camera' AND cp.scope_id=c.id "
        "LEFT JOIN recording_policy_overrides gp "
        "ON gp.scope_type='group' AND gp.scope_id=c.group_id WHERE c.id=?", (camera_id,)
    ).fetchone()
    if row is None:
        return {"mode": "disabled", "source": "camera", "timezone": None, "intervals": [], "scheduled": False}
    if row["camera_schedule"] is not None:
        timezone_name = row["camera_timezone"]
        intervals = json.loads(row["camera_schedule"])
        mode, source = mode_at(intervals, timezone_name, instant), "camera"
        scheduled = True
    elif row["group_schedule"] is not None:
        timezone_name = row["group_timezone"]
        intervals = json.loads(row["group_schedule"])
        mode, source = mode_at(intervals, timezone_name, instant), "group"
        scheduled = True
    else:
        timezone_name, intervals = None, []
        mode, source = row["recording_mode"], "camera"
        scheduled = False
    if not row["enabled"] or not row["recording_enabled"]:
        mode = "disabled"
    held = None
    if mode != "disabled":
        from .failover import held_reason

        held = held_reason(db)  # A standby that has not taken over, or a primary waiting for the standby, does not record
        if held:
            mode = "disabled"
    return {"mode": mode, "source": source, "timezone": timezone_name, "intervals": intervals, "scheduled": scheduled, **({"held": held} if held else {})}
