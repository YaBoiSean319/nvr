"""Activity insights (#135): counts of people and vehicles per camera, zone and line by hour or day, and where tracks go.

Everything is counted from the stored tracks. A camera's count is the tracks that began in each hour or day. A zone or line is one an
administrator drew in an enabled rule (zone entry, line crossing) for that camera: a track counts once for a zone, when it first enters,
and once for each crossing of a line (in the rule's direction), so an object that walks back and forth counts each way. The rule's schedule,
cooldown and alarm settings do not matter here, only its shape and the classes it watches.

Every answer says how much of each hour or day the AI was analysing the camera, so a low count in time nobody looked at is not read as a quiet
time: those hours are marked in the charts and in the CSV.
"""
from __future__ import annotations

import bisect
import csv
import io
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import rules, search

MAX_RANGE = {"hour": timedelta(days=31), "day": search.MAX_RANGE}
LABELS = search.LABELS
HEAT_COLUMNS, HEAT_ROWS = 32, 18
UNANALYSED_NOTE = "Not fully analysed: counts may be lower than what happened"
CHUNK = 500


def site_zone(db: sqlite3.Connection) -> ZoneInfo:
    row = db.execute("SELECT time_zone FROM site_display_settings WHERE singleton_id=1").fetchone()
    try:
        return ZoneInfo(row["time_zone"] if row else "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def buckets(start: datetime, end: datetime, zone: ZoneInfo, unit: str) -> list[tuple[datetime, datetime]]:
    """The hours or days (on the site's clock) that [start, end) touches, as UTC intervals."""
    local = start.astimezone(zone)
    result = []
    if unit == "hour":
        # Hours are exact: a clock change makes one hour repeat or vanish on the clock, not stretch.
        cursor = local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        while cursor < end:
            result.append((cursor, cursor + timedelta(hours=1)))
            cursor += timedelta(hours=1)
        return result
    # Days follow the clock, so a day with a clock change is 23 or 25 hours long.
    cursor_day = local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    while cursor_day.replace(tzinfo=zone).astimezone(timezone.utc) < end:
        following = cursor_day + timedelta(days=1)
        result.append((cursor_day.replace(tzinfo=zone).astimezone(timezone.utc), following.replace(tzinfo=zone).astimezone(timezone.utc)))
        cursor_day = following
    return result


def _analysed_intervals(db: sqlite3.Connection, camera_ids: list[str], start: datetime, end: datetime) -> dict[str, list[tuple[datetime, datetime]]]:
    found: dict[str, list[tuple[datetime, datetime]]] = {camera_id: [] for camera_id in camera_ids}
    for offset in range(0, len(camera_ids), CHUNK):
        part = camera_ids[offset:offset + CHUNK]
        for row in db.execute(f"SELECT camera_id,starts_at,ends_at FROM ai_coverage WHERE state='analyzed' AND camera_id IN ({','.join('?' * len(part))}) "
                              "AND starts_at<? AND (ends_at IS NULL OR ends_at>?) ORDER BY starts_at", (*part, _iso(end), _iso(start))):
            begin, finish = max(start, _parse(row["starts_at"])), min(end, _parse(row["ends_at"]) if row["ends_at"] else end)
            if finish > begin:
                found[row["camera_id"]].append((begin, finish))
    return {camera_id: search._merge(intervals) for camera_id, intervals in found.items()}


def _analysed_percent(intervals: list[tuple[datetime, datetime]], begin: datetime, finish: datetime, now: datetime) -> float | None:
    """How much of the part of [begin, finish) that has happened was analysed; None for an hour that has not started."""
    finish = min(finish, now)
    if finish <= begin:
        return None
    covered = sum((min(b, finish) - max(a, begin)).total_seconds() for a, b in intervals if a < finish and b > begin)
    return round(100 * covered / (finish - begin).total_seconds(), 1)


def definitions(db: sqlite3.Connection, camera_ids: list[str]) -> list[dict[str, object]]:
    """The zones and lines of enabled rules on these cameras, one per rule and camera, in rule order."""
    result = []
    groups: dict[str, set[str]] = {}
    for rule in rules.load_rules(db):
        if rule.condition_type not in ("zone_entry", "line_crossing"):
            continue
        for camera_id in camera_ids:
            if camera_id not in groups:
                groups[camera_id] = rules._camera_groups(db, camera_id)
            if rule.watches_camera(camera_id, groups[camera_id]):
                result.append({"rule": rule, "camera_id": camera_id})
    return result


def counts(db: sqlite3.Connection, cameras: dict[str, str], start: datetime, end: datetime, unit: str, *, label: str | None = None,
           now: datetime | None = None) -> dict[str, object]:
    """Counts per bucket for each camera, zone and line, with how much of each bucket was analysed."""
    now = now or datetime.now(timezone.utc)
    zone = site_zone(db)
    spans = buckets(start, end, zone, unit)
    starts = [span[0] for span in spans]
    labels = [label] if label else list(LABELS)

    def index(moment: datetime) -> int | None:
        position = bisect.bisect_right(starts, moment) - 1
        return position if 0 <= position < len(spans) and moment < spans[position][1] and start <= moment < end else None

    subjects: list[dict[str, object]] = []
    series: dict[str, list[list[int]]] = {}

    def subject(subject_id: str, kind: str, name: str, camera_id: str, extra: dict[str, object] | None = None) -> list[list[int]]:
        subjects.append({"id": subject_id, "kind": kind, "name": name, "camera_id": camera_id, "camera_name": cameras[camera_id], **(extra or {})})
        series[subject_id] = [[0] * len(LABELS) for _ in spans]
        return series[subject_id]

    ids = list(cameras)
    defined: dict[str, list[tuple[rules.Rule, list[list[int]]]]] = defaultdict(list)
    for camera_id in ids:
        subject(f"camera:{camera_id}", "camera", cameras[camera_id], camera_id)
    for entry in definitions(db, ids):
        rule, camera_id = entry["rule"], str(entry["camera_id"])
        kind = "zone" if rule.condition_type == "zone_entry" else "line"
        geometry = rule.condition["zone" if kind == "zone" else "line"]
        defined[camera_id].append((rule, subject(f"{kind}:{rule.id}:{camera_id}", kind, rule.name, camera_id, {"geometry": geometry, "rule_id": rule.id,
                                                                                                               "direction": rule.condition.get("direction")})))

    for camera_id in ids:
        # A track that began before the range can still enter a zone or cross a line inside it, so those are found from every track that overlaps the range.
        track_rows = db.execute("SELECT id,label,first_seen_at FROM ai_tracks WHERE camera_id=? AND last_seen_at>=? AND first_seen_at<? ORDER BY first_seen_at",
                                (camera_id, _iso(start), _iso(end))).fetchall()
        tracks = [row for row in track_rows if row["label"] in labels]
        own = series[f"camera:{camera_id}"]
        for row in tracks:
            position = index(_parse(row["first_seen_at"]))  # a camera's count is the tracks that began in the hour
            if position is not None:
                own[position][LABELS.index(row["label"])] += 1
        if not defined[camera_id] or not tracks:
            continue
        watched = set().union(*(rule.classes for rule, _ in defined[camera_id]))
        tracks = [row for row in tracks if row["label"] in watched]  # only the paths a rule could use are read
        paths: dict[str, list[sqlite3.Row]] = defaultdict(list)
        track_ids = [row["id"] for row in tracks]
        for offset in range(0, len(track_ids), CHUNK):
            part = track_ids[offset:offset + CHUNK]
            for detection in db.execute(f"SELECT track_id,id,detected_at,box_x,box_y,box_width,box_height FROM ai_detections WHERE track_id IN ({','.join('?' * len(part))}) "
                                        "ORDER BY track_id,detected_at,id", part):
                paths[detection["track_id"]].append(detection)
        for row in tracks:
            path = rules.path_from_rows(paths[row["id"]])
            for rule, cells in defined[camera_id]:
                if row["label"] not in rule.classes:
                    continue
                moments = rules.condition_moments(rule, path)
                if rule.condition_type == "zone_entry":
                    moments = moments[:1]  # once per track
                for moment in moments:
                    position = index(moment.at)
                    if position is not None:
                        cells[position][LABELS.index(row["label"])] += 1

    analysed = _analysed_intervals(db, ids, start, end)
    coverage = {camera_id: [_analysed_percent(analysed[camera_id], begin, finish, now) for begin, finish in spans] for camera_id in ids}
    return {"unit": unit, "time_zone": zone.key, "labels": list(LABELS), "shown_labels": labels,
            "buckets": [{"start": _iso(begin), "end": _iso(finish)} for begin, finish in spans],
            "subjects": subjects, "counts": series, "analysed": coverage, "coverage": search.coverage(db, cameras, start, min(end, now))}


def _safe(value: str) -> str:
    """A cell a spreadsheet will not run as a formula."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def to_csv(report: dict[str, object]) -> str:
    """One row per hour or day and camera, zone or line, with the share analysed and a note where it was not all of it."""
    zone = ZoneInfo(str(report["time_zone"]))
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    shown = list(report["shown_labels"])  # type: ignore[arg-type]
    writer.writerow(["bucket_start_local", "bucket_start_utc", "kind", "name", "camera", *shown, "analysed_percent", "note"])
    for subject in report["subjects"]:  # type: ignore[attr-defined]
        cells = report["counts"][subject["id"]]  # type: ignore[index]
        percents = report["analysed"][subject["camera_id"]]  # type: ignore[index]
        for position, span in enumerate(report["buckets"]):  # type: ignore[arg-type]
            begin = _parse(span["start"])
            percent = percents[position]
            note = "Not yet" if percent is None else "" if percent >= 99.95 else UNANALYSED_NOTE
            writer.writerow([begin.astimezone(zone).isoformat(timespec="minutes"), _iso(begin), subject["kind"], _safe(str(subject["name"])), _safe(str(subject["camera_name"])),
                             *(cells[position][LABELS.index(name)] for name in shown), "" if percent is None else percent, note])
    return out.getvalue()


def heatmap(db: sqlite3.Connection, camera_id: str, camera_name: str, start: datetime, end: datetime, *, label: str | None = None,
            now: datetime | None = None) -> dict[str, object]:
    """Where objects were over the period: how many times an object's centre was sampled in each cell of a 32 x 18 grid of the frame."""
    now = now or datetime.now(timezone.utc)
    clauses, values = ["d.camera_id=?", "d.detected_at>=?", "d.detected_at<?"], [camera_id, _iso(start), _iso(end)]
    if label:
        clauses.append("t.label=?")
        values.append(label)
    cells = [[0] * HEAT_COLUMNS for _ in range(HEAT_ROWS)]
    for row in db.execute(
        f"SELECT min(CAST((d.box_x+d.box_width/2)*{HEAT_COLUMNS} AS INTEGER),{HEAT_COLUMNS - 1}) AS column,"
        f"min(CAST((d.box_y+d.box_height/2)*{HEAT_ROWS} AS INTEGER),{HEAT_ROWS - 1}) AS row,COUNT(*) AS samples "
        f"FROM ai_detections d JOIN ai_tracks t ON t.id=d.track_id WHERE {' AND '.join(clauses)} GROUP BY column,row", values):
        cells[row["row"]][row["column"]] = row["samples"]
    tracks = db.execute(f"SELECT COUNT(DISTINCT d.track_id) FROM ai_detections d JOIN ai_tracks t ON t.id=d.track_id WHERE {' AND '.join(clauses)}", values).fetchone()[0]
    return {"camera_id": camera_id, "camera_name": camera_name, "columns": HEAT_COLUMNS, "rows": HEAT_ROWS, "cells": cells,
            "peak": max((max(row) for row in cells), default=0), "tracks": tracks, "label": label,
            "coverage": search.coverage(db, {camera_id: camera_name}, start, min(end, now))}
