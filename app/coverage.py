"""Coverage gaps and why they happened, and the weekly coverage report.

A gap is any stretch of a camera's timeline without indexed footage. Each one carries the
coverage state the recorder observed there and a reason code. Playback's timeline and the
coverage report both compute gaps here, so they always agree.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .store import connect

# Why a gap happened. The first seven are the outages an operator acts on; no_motion is a
# motion camera with nothing to record, which is expected rather than an outage.
REASONS = {
    "camera_offline": "Camera offline",
    "stream_error": "Stream error",
    "disk_unavailable": "Disk full or unavailable",
    "outside_schedule": "Outside schedule",
    "recording_disabled": "Recording disabled",
    "worker_restart": "Recorder restarting",
    "no_motion": "No motion",
    "unknown": "Unknown",
}
# Gaps the camera's policy expects: they do not count against its coverage.
EXPECTED = frozenset({"outside_schedule", "recording_disabled", "no_motion"})
STREAM_FAULTS = frozenset({"camera_offline", "stream_error", "disk_unavailable"})
REPORT_GAPS = 5


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def reason_code(camera: sqlite3.Row | dict[str, Any], policy: dict[str, Any], health: dict[str, Any], storage_faulty: bool) -> str | None:
    """The reason for the coverage the recorder sees now, or None while footage is being covered."""
    if health["coverage_kind"] == "covered":
        return None
    if not camera["enabled"] or not camera["recording_enabled"]:
        return "recording_disabled"
    if policy["mode"] == "disabled":
        return "outside_schedule" if policy.get("scheduled") else "recording_disabled"
    if health["state"] == "expected_idle":
        return "no_motion"
    if storage_faulty or camera["stream_fault"] == "disk_unavailable":
        return "disk_unavailable"
    if camera["stream_fault"] in STREAM_FAULTS:
        return camera["stream_fault"]
    return "unknown"


def timeline(db: sqlite3.Connection, camera_id: str, start: datetime, end: datetime, limit: int | None = 500) -> dict[str, list[dict[str, Any]]]:
    """A camera's footage, recorded coverage intervals and gaps in [start, end), in UTC."""
    start_text, end_text = start.isoformat(), end.isoformat()
    bound = limit if limit is not None else -1  # SQLite: a negative LIMIT is no limit.
    segments = db.execute(
        "SELECT id,starts_at,ends_at,size_bytes FROM recording_segments "
        "WHERE camera_id=? AND starts_at<? AND ends_at>? AND unavailable_at IS NULL ORDER BY starts_at LIMIT ?",
        (camera_id, end_text, start_text, bound)).fetchall()
    coverage = [dict(row) for row in db.execute(
        "SELECT coverage_kind,reason,reason_code,starts_at,ends_at FROM coverage_intervals "
        "WHERE camera_id=? AND starts_at<? AND (ends_at IS NULL OR ends_at>?) ORDER BY starts_at LIMIT ?",
        (camera_id, end_text, start_text, bound))]
    # Indexed footage whose file is gone (for example after a restore) is a known failure, not footage.
    coverage += [{"coverage_kind": "failure", "reason": row["unavailable_reason"], "reason_code": "disk_unavailable",
                  "starts_at": row["starts_at"], "ends_at": row["ends_at"]}
                 for row in db.execute(
                     "SELECT starts_at,ends_at,unavailable_reason FROM recording_segments "
                     "WHERE camera_id=? AND starts_at<? AND ends_at>? AND unavailable_at IS NOT NULL ORDER BY starts_at LIMIT ?",
                     (camera_id, end_text, start_text, bound))]
    spans = sorted((max(start, _parse(row["starts_at"])), min(end, _parse(row["ends_at"]))) for row in segments)
    return {"segments": [dict(row) for row in segments], "coverage": coverage, "gaps": gaps(coverage, spans, start, end)}


def gaps(coverage: list[dict[str, Any]], spans: Iterable[tuple[datetime, datetime]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Every stretch of [start, end) outside ``spans``, split where the recorded coverage changes."""
    result: list[dict[str, Any]] = []
    normalized_coverage = [(_parse(item["starts_at"]), _parse(item["ends_at"]) if item["ends_at"] else end, item) for item in coverage]

    def append_gap(gap_start: datetime, gap_end: datetime) -> None:
        if gap_end <= gap_start:
            return
        boundaries = {gap_start, gap_end}
        normalized = []
        for item_start, item_end, item in normalized_coverage:
            item_start, item_end = max(gap_start, item_start), min(gap_end, item_end)
            if item_end > item_start:
                boundaries.update((item_start, item_end))
                normalized.append((item_start, item_end, item))
        ordered = sorted(boundaries)
        for piece_start, piece_end in zip(ordered, ordered[1:]):
            midpoint = piece_start + (piece_end - piece_start) / 2
            active = [item for item_start, item_end, item in normalized if item_start <= midpoint < item_end]
            kinds = {item["coverage_kind"] for item in active}
            kind = "failure" if "failure" in kinds else "uncertain" if "uncertain" in kinds or "covered" in kinds else "expected_idle" if "expected_idle" in kinds else "missing"
            source = next((item for item in active if item["coverage_kind"] == kind), None)
            reason = source["reason"] if source else ("Coverage is uncertain where recording is expected" if kind == "uncertain" else "No indexed footage covers this interval")
            code = (source.get("reason_code") if source else None) or "unknown"
            result.append({"starts_at": piece_start.isoformat(), "ends_at": piece_end.isoformat(), "state": kind, "reason": reason, "reason_code": code})

    cursor = start
    for span_start, span_end in spans:
        if span_start > cursor:
            append_gap(cursor, span_start)
        cursor = max(cursor, span_end)
    if cursor < end:
        append_gap(cursor, end)
    return result


def _seconds(items: Iterable[dict[str, Any]]) -> float:
    return sum((_parse(gap["ends_at"]) - _parse(gap["starts_at"])).total_seconds() for gap in items)


def report(start: datetime, end: datetime) -> dict[str, Any]:
    """Coverage per camera against its policy for [start, end), with its largest outages and their reasons."""
    from .scheduling import effective_policy

    # Only time that has happened counts, and only since each camera was added.
    start, end = start.astimezone(timezone.utc), min(end.astimezone(timezone.utc), datetime.now(timezone.utc))
    cameras = []
    with connect() as db:
        rows = db.execute("SELECT id,name,recording_mode,created_at FROM cameras WHERE archived_at IS NULL ORDER BY name COLLATE NOCASE, id").fetchall()
        for camera in rows:
            since = max(start, _parse(camera["created_at"]))
            if since >= end:
                continue
            camera_gaps = timeline(db, camera["id"], since, end, limit=None)["gaps"]
            expected_idle = [gap for gap in camera_gaps if gap["reason_code"] in EXPECTED]
            outages = [gap for gap in camera_gaps if gap["reason_code"] not in EXPECTED]
            expected = (end - since).total_seconds() - _seconds(expected_idle)
            missed = _seconds(outages)
            by_reason: dict[str, float] = {}
            for gap in outages:
                by_reason[gap["reason_code"]] = by_reason.get(gap["reason_code"], 0) + _seconds([gap])
            policy = effective_policy(db, camera["id"], end)
            cameras.append({
                "camera_id": camera["id"], "name": camera["name"],
                "policy": {"mode": camera["recording_mode"], "source": policy["source"], "scheduled": policy["scheduled"]},
                "expected_seconds": round(expected, 1), "covered_seconds": round(expected - missed, 1), "missed_seconds": round(missed, 1),
                "coverage_percent": None if expected <= 0 else round(100 * (expected - missed) / expected, 2),
                "missed_by_reason": {code: round(value, 1) for code, value in sorted(by_reason.items(), key=lambda item: -item[1])},
                "largest_gaps": sorted(({**gap, "seconds": round(_seconds([gap]), 1)} for gap in outages), key=lambda gap: -gap["seconds"])[:REPORT_GAPS],
            })
    return {"start": start.isoformat(), "end": end.isoformat(), "reasons": REASONS, "cameras": cameras}


def last_week(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The most recent full week, Monday to Monday at midnight in the site's time zone."""
    from .backup import site_zone

    zone = site_zone()
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    monday = datetime.combine((local - timedelta(days=local.weekday())).date(), datetime.min.time(), zone)
    return (monday - timedelta(days=7)).astimezone(timezone.utc), monday.astimezone(timezone.utc)


def publish_weekly_report(now: datetime | None = None) -> dict[str, Any] | None:
    """Store last week's report once and send it to webhooks subscribed to ``coverage.weekly_report``."""
    from .webhooks import enqueue_event

    period_start, period_end = last_week(now)
    with connect() as db:
        if db.execute("SELECT 1 FROM coverage_reports WHERE period_start=? AND period_end=?", (period_start.isoformat(), period_end.isoformat())).fetchone():
            return None
    weekly = report(period_start, period_end)
    with connect() as db:
        # Stored and queued together, so a report is never kept without having been sent.
        db.execute("BEGIN IMMEDIATE")
        try:
            created = db.execute("INSERT OR IGNORE INTO coverage_reports(period_start,period_end,created_at,report_json) VALUES(?,?,?,?)",
                                 (period_start.isoformat(), period_end.isoformat(), datetime.now(timezone.utc).isoformat(), json.dumps(weekly)))
            if created.rowcount:
                enqueue_event(db, "coverage.weekly_report", {"event_id": str(uuid.uuid4()), "event_type": "coverage.weekly_report",
                                                             "occurred_at": datetime.now(timezone.utc).isoformat(), "report": weekly})
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return weekly if created.rowcount else None


def latest_reports(limit: int = 8) -> list[dict[str, Any]]:
    with connect() as db:
        return [{"id": row["id"], "period_start": row["period_start"], "period_end": row["period_end"], "created_at": row["created_at"],
                 "report": json.loads(row["report_json"])}
                for row in db.execute("SELECT * FROM coverage_reports ORDER BY period_start DESC LIMIT ?", (limit,))]
