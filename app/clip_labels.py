"""Motion clip labels (#254): what the AI found in the time a motion clip covers.

The AI worker already follows people and vehicles on every analysed camera, so a motion clip is labelled from the tracks that overlap its
time: the classes seen, or "nothing" when the AI was analysing the camera for the whole clip and saw no one. A clip the AI was not
analysing for all of its time (AI off, paused, delayed or failed) gets no label at all, because silence there would not mean nothing happened.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

LABEL_ORDER = ("person", "vehicle")


def _parse(value: str) -> datetime:
    from datetime import timezone
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def event_span(event: dict[str, Any]) -> tuple[datetime, datetime]:
    """The time a motion event's clip covers: what was recorded, else what was asked for, else the moment of the event."""
    start = event.get("actual_start") or event.get("requested_start") or event["source_event_at"]
    end = event.get("actual_end") or event.get("requested_end") or event["source_event_at"]
    first, last = _parse(start), _parse(end)
    return (first, last) if last >= first else (last, first)


def label_events(events: Iterable[dict[str, Any]], tracks: list[dict[str, Any]], analysed: list[dict[str, Any]]) -> None:
    """Add ``ai`` to each event: ``{"classes": [...], "checked": bool}``.

    ``tracks`` have label, first_seen_at, last_seen_at. ``analysed`` are the camera's intervals of analysed time (starts_at, ends_at, ends_at empty while
    one is still running): the AI records only the time it was analysing, so a clip is checked only when one interval covers all of it.
    """
    seen = [(track["label"], _parse(track["first_seen_at"]), _parse(track["last_seen_at"])) for track in tracks]
    covered = _merged([(_parse(item["starts_at"]), _parse(item["ends_at"]) if item.get("ends_at") else None) for item in analysed])
    for event in events:
        start, end = event_span(event)
        # A few seconds either side: a person walking into view is tracked a moment before or after the camera's motion.
        window_start, window_end = start - timedelta(seconds=3), end + timedelta(seconds=3)
        classes = sorted({label for label, first, last in seen if first <= window_end and last >= window_start},
                         key=lambda label: LABEL_ORDER.index(label) if label in LABEL_ORDER else len(LABEL_ORDER))
        checked = any(first <= start and (last is None or last >= end) for first, last in covered)
        event["ai"] = {"classes": classes, "checked": checked}


def _merged(intervals: list[tuple[datetime, datetime | None]]) -> list[tuple[datetime, datetime | None]]:
    """Intervals that touch or overlap joined into one (an open end stays open)."""
    result: list[tuple[datetime, datetime | None]] = []
    for first, last in sorted(intervals, key=lambda item: item[0]):
        if result and (result[-1][1] is None or first <= result[-1][1]):
            previous_last = result[-1][1]
            result[-1] = (result[-1][0], None if previous_last is None or last is None else max(previous_last, last))
        else:
            result.append((first, last))
    return result
