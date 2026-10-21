"""Validated site-wide display preferences and local-time conversion."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DATE_FORMATS = {"friendly", "long", "iso", "day-month", "month-day"}
CLOCK_FORMATS = {"12", "24"}


def validate_display_settings(time_zone: str, date_format: str, clock_format: str) -> ZoneInfo:
    if date_format not in DATE_FORMATS:
        raise ValueError("Choose one of the supported date formats")
    if clock_format not in CLOCK_FORMATS:
        raise ValueError("Choose a 12-hour or 24-hour clock")
    try:
        return ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Choose a valid IANA time zone, such as America/New_York") from exc


def local_time_candidates(value: datetime, time_zone: str) -> list[datetime]:
    """Return UTC instants for a naive local time, in chronological order.

    A spring-forward wall time has no candidates. A fall-back wall time has two.
    """
    if value.tzinfo is not None:
        raise ValueError("Local search times must not include a timezone offset")
    zone = validate_display_settings(time_zone, "friendly", "12")
    candidates: set[datetime] = set()
    for fold in (0, 1):
        local = value.replace(tzinfo=zone, fold=fold)
        utc = local.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == value:
            candidates.add(utc)
    return sorted(candidates)


def format_display_datetime(value: datetime, time_zone: str, date_format: str, clock_format: str) -> str:
    zone = validate_display_settings(time_zone, date_format, clock_format)
    local = value.astimezone(zone)
    if date_format == "friendly":
        date = f"{local.strftime('%b')} {local.day}, {local.year}"
    elif date_format == "long":
        date = f"{local.strftime('%B')} {local.day}, {local.year}"
    elif date_format == "iso":
        date = local.strftime("%Y-%m-%d")
    elif date_format == "day-month":
        date = local.strftime("%d/%m/%Y")
    else:
        date = local.strftime("%m/%d/%Y")
    if clock_format == "24":
        clock = local.strftime("%H:%M:%S")
    else:
        clock = local.strftime("%I:%M:%S %p").lstrip("0")
    return f"{date} {clock}"
