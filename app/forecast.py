"""How long each camera's footage will really be kept, against how long its policy says.

Footage is removed only when it is older than its camera's retention policy; nothing removes it to make room. So when the cameras on a
disk are writing faster than the disk can hold their policies' worth, the disk fills, and what the policy promised is not what is kept.
This works out, from each camera's recent write rate, what the disk can hold:

- a **write rate** per camera: the bytes of footage indexed in the last 48 hours over the time it has been recording in that window (at least
  ``MIN_HISTORY`` of footage is needed before it forecasts);
- a **budget** per disk: its free space (less the reserve below which the NVR reports storage pressure) plus the footage already stored on it by
  the cameras that write to it, less footage under a hold. Volumes on the same disk share one budget;
- the **retention a camera keeps**: if every camera's policy fits in the budget, its policy. If not, the disk's cameras share the shortfall by
  each keeping the same number of days, no more than its policy (the water-filling level ``D`` with ``sum(rate * min(policy, D)) = budget``);
- **when the shortfall begins**: footage grows towards each policy at the camera's write rate, so the days until the stored footage first exceeds the budget.

A camera whose disk cannot hold its policy shows "Camera X's disk can hold about N days at the current write rate, below its M-day policy", with the changes that would fix it,
and raises an incident when the shortfall begins within ``WARN_DAYS`` (7 days).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .incidents import sync_incident

WINDOW = timedelta(hours=48)
MIN_HISTORY = timedelta(hours=6)
WARN_DAYS = 7
SEGMENT_SLACK = timedelta(hours=1)  # No segment is longer than this, so one that ends in the window started within an hour of it.
TOLERANCE = 0.005  # A forecast within half a percent of the policy is the policy.


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def reserve_bytes() -> int:
    return int(os.environ.get("NVR_STORAGE_PRESSURE_FREE_BYTES", "1073741824"))


def water_level(rates: list[float], policies: list[float], budget: float) -> float | None:
    """The common number of days D with sum(rate * min(policy, D)) == budget, or None when every policy fits."""
    if sum(r * p for r, p in zip(rates, policies)) <= budget:
        return None
    ordered = sorted(zip(policies, rates))
    spent, remaining_rate, previous = 0.0, sum(rates), 0.0
    for policy, rate in ordered:
        reach = previous + (budget - spent) / remaining_rate if remaining_rate else float("inf")
        if reach <= policy:
            return max(0.0, reach)
        spent += remaining_rate * (policy - previous)
        previous = policy
        remaining_rate -= rate
    return previous


def days_until_shortfall(rates: list[float], policies: list[float], ages: list[float], budget: float) -> float | None:
    """Days from now until the footage stored first exceeds the budget, as each camera's footage grows to its policy; None if it never does."""
    def stored(days: float) -> float:
        return sum(r * min(p, a + days) for r, p, a in zip(rates, policies, ages))
    if stored(0) > budget:
        return 0.0
    if stored(max(policies, default=0)) <= budget:
        return None
    # Piecewise linear and increasing: walk the breakpoints where a camera reaches its policy.
    points = sorted({0.0, *(max(0.0, p - a) for p, a in zip(policies, ages))})
    for left, right in zip(points, points[1:] + [points[-1] + max(policies)]):
        low, high = stored(left), stored(right)
        if high > budget >= low:
            return left + (budget - low) / (high - low) * (right - left) if high > low else left
    return None


def measure(db: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """Each camera's policy, write rate, stored footage and age, as the forecast needs them (a few grouped queries, whatever the camera count)."""
    from .retention import effective_retention
    window_start = (now - WINDOW).isoformat()
    earliest_start = (now - WINDOW - SEGMENT_SLACK).isoformat()  # Keeps the recent-footage scan off the whole of a camera's history.
    totals = {row["camera_id"]: row for row in db.execute(
        "SELECT camera_id,COALESCE(SUM(size_bytes),0) AS stored,MIN(starts_at) AS first FROM recording_segments WHERE unavailable_at IS NULL GROUP BY camera_id")}
    recent = {row["camera_id"]: row["written"] for row in db.execute(
        "SELECT camera_id,COALESCE(SUM(size_bytes),0) AS written FROM recording_segments WHERE unavailable_at IS NULL AND starts_at>=? AND ends_at>=? GROUP BY camera_id",
        (earliest_start, window_start))}
    # Footage under a hold is never removed by age, so it is not room the disk will get back.
    held = {row["camera_id"]: row["held"] for row in db.execute(
        "SELECT s.camera_id,COALESCE(SUM(s.size_bytes),0) AS held FROM recording_segments s WHERE s.unavailable_at IS NULL AND EXISTS("
        "SELECT 1 FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>?)) GROUP BY s.camera_id",
        (now.astimezone(timezone.utc).isoformat(),))}
    volume_ids = {row["id"]: row["storage_volume_id"] for row in db.execute("SELECT c.id,g.storage_volume_id FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id")}
    cameras = []
    for camera in db.execute("SELECT id,name,archived_at FROM cameras ORDER BY name,id").fetchall():
        total = totals.get(camera["id"])
        try:
            history = max(timedelta(0), now - _parse(total["first"])) if total and total["first"] else timedelta(0)
        except ValueError:
            history = timedelta(0)
        span = min(WINDOW, history)
        policy, source = effective_retention(db, camera["id"])
        collecting = camera["archived_at"] is not None or span < MIN_HISTORY
        cameras.append({
            "id": camera["id"], "name": camera["name"], "volume_id": volume_ids.get(camera["id"]) or "default", "policy_days": policy, "policy_source": source,
            "stored_bytes": int(total["stored"]) if total else 0, "held_bytes": int(held.get(camera["id"], 0)), "age_days": min(policy, history.total_seconds() / 86400),
            "write_bytes_per_day": 0.0 if collecting else recent.get(camera["id"], 0) / (span.total_seconds() / 86400), "collecting": collecting, "archived": camera["archived_at"] is not None,
        })
    return cameras


def _shape_days(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def forecast(db: sqlite3.Connection, now: datetime | None = None, usage: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free,
             device_of: Callable[[Path], Any] = lambda path: os.stat(path).st_dev) -> dict[str, Any]:
    """The forecast for every camera and every disk, now."""
    from .storage import configured_volumes, volume_path
    now = now or datetime.now(timezone.utc)
    measured = measure(db, now)
    volumes: dict[str, dict[str, Any]] = {}
    groups: dict[Any, list[str]] = {}
    for volume_id in configured_volumes():
        try:
            path = volume_path(volume_id)
            if volume_id == "default":
                # Recording creates it on first use: until then the disk it will be on is the one its nearest existing parent is on.
                while not path.exists() and path != path.parent:
                    path = path.parent
            key, free = device_of(path), usage(path)
        except OSError:
            volumes[volume_id] = {"id": volume_id, "available": False, "free_bytes": None, "group": None}
            continue
        volumes[volume_id] = {"id": volume_id, "available": True, "free_bytes": free, "group": key}
        groups.setdefault(key, []).append(volume_id)
    reserve = reserve_bytes()
    disks, cameras = [], []
    by_volume: dict[str, list[dict[str, Any]]] = {}
    for camera in measured:
        by_volume.setdefault(camera["volume_id"], []).append(camera)
    headroom: dict[Any, float] = {}
    disk_of: dict[str, dict[str, Any]] = {}
    for key, ids in groups.items():
        members = [camera for volume_id in ids for camera in by_volume.get(volume_id, [])]
        budget = max(0, volumes[ids[0]]["free_bytes"] - reserve) + sum(camera["stored_bytes"] - camera["held_bytes"] for camera in members)
        active = [camera for camera in members if camera["write_bytes_per_day"] > 0]
        rates, policies = [c["write_bytes_per_day"] for c in active], [float(c["policy_days"]) for c in active]
        ages = [c["age_days"] for c in active]
        needed = sum(r * p for r, p in zip(rates, policies))
        level = water_level(rates, policies, budget) if active else None
        until = days_until_shortfall(rates, policies, ages, budget) if active else None
        disk = {"volume_ids": ids, "free_bytes": volumes[ids[0]]["free_bytes"], "budget_bytes": budget, "needed_bytes": round(needed), "fits": level is None,
                "shortfall_bytes": max(0, round(needed - budget)), "level_days": _shape_days(level), "days_until_shortfall": _shape_days(until)}
        disks.append(disk)
        headroom[key] = budget - needed
        for volume_id in ids:
            disk_of[volume_id] = {"key": key, "level": level, "until": until, "budget": budget, "active": active, "rates": rates, "policies": policies}
    names = {volume_id: [volume_id] for volume_id in volumes}
    for camera in measured:
        entry = {key: camera[key] for key in ("id", "name", "volume_id", "policy_days", "policy_source", "stored_bytes")}
        entry.update(write_bytes_per_day=round(camera["write_bytes_per_day"]), age_days=round(camera["age_days"], 2))
        context = disk_of.get(camera["volume_id"])
        if camera["archived"] or camera["collecting"] or context is None or camera["write_bytes_per_day"] == 0:
            entry.update(state="not_forecast", forecast_days=None, below_policy=False, days_until_shortfall=None, suggestions=[],
                         message=("Archived: its footage ages out under its policy." if camera["archived"] else
                                  "The volume this camera records to is not available." if context is None else
                                  "Collecting data: a forecast needs at least 6 hours of footage." if camera["collecting"] else
                                  "Not writing: no footage was recorded in the last 48 hours."))
            cameras.append(entry)
            continue
        level = context["level"]
        kept = float(camera["policy_days"]) if level is None else min(float(camera["policy_days"]), level)
        below = kept < camera["policy_days"] * (1 - TOLERANCE)
        entry.update(forecast_days=_shape_days(kept), below_policy=below, days_until_shortfall=_shape_days(context["until"]) if below else None, state="below_policy" if below else "ok")
        entry["message"] = (f"{camera['name']}'s disk can hold about {kept:.0f} days at the current write rate, below its {camera['policy_days']}-day policy" if below
                            else f"{camera['name']} keeps its {camera['policy_days']}-day policy")
        entry["suggestions"] = suggest(camera, measured, context, volumes, groups, headroom, reserve) if below else []
        cameras.append(entry)
    return {"as_of": now.isoformat(), "window_hours": int(WINDOW.total_seconds() // 3600), "warn_days": WARN_DAYS, "reserve_bytes": reserve, "disks": disks, "cameras": cameras,
            "note": "Footage is removed only when it passes its policy, never to make room: when the disk fills first, recording on it stops. Estimates use the last 48 hours of writing, and footage under a hold is not counted as room."}


def suggest(camera: dict[str, Any], measured: list[dict[str, Any]], context: dict[str, Any], volumes: dict[str, dict[str, Any]], groups: dict[Any, list[str]],
            headroom: dict[Any, float], reserve: int) -> list[str]:
    """What would bring this camera back to its policy: a lower bitrate, another volume, or shorter policies for the others."""
    suggestions = []
    others = [c for c in context["active"] if c["id"] != camera["id"]]
    spent_by_others = sum(c["write_bytes_per_day"] * c["policy_days"] for c in others)
    room = context["budget"] - spent_by_others
    if room > 0 and camera["write_bytes_per_day"] > 0:
        cut = 1 - (room / camera["policy_days"]) / camera["write_bytes_per_day"]
        if cut > 0:
            suggestions.append(f"Lower {camera['name']}'s bitrate by about {cut * 100:.0f}% (to about {room / camera['policy_days'] / 1e9:.1f} GB a day) so it fits with the other cameras on its disk.")
    need = camera["write_bytes_per_day"] * camera["policy_days"]
    for key, spare in headroom.items():
        if key != context["key"] and spare >= need:
            names = ", ".join(groups[key])
            suggestions.append(f"Move {camera['name']} to the volume {names}: its disk has room for about {spare / 1e9:.0f} GB more than its cameras need.")
            break
    longer = sorted((c for c in measured if c["id"] != camera["id"] and c["volume_id"] in groups.get(context["key"], []) and not c["collecting"] and c["write_bytes_per_day"] > 0 and c["policy_days"] > (context["level"] or 0)),
                    key=lambda c: -c["policy_days"])
    if longer and context["level"] is not None:
        suggestions.append("Shorten the retention of " + ", ".join(f"{c['name']} ({c['policy_days']} days)" for c in longer[:3]) + f" to about {context['level']:.0f} days.")
    return suggestions


def sync_incidents(db: sqlite3.Connection, result: dict[str, Any]) -> None:
    """An incident for each camera that will fall below its policy within a week, recovering when it no longer will."""
    seen = set()
    for camera in result["cameras"]:
        key = f"retention:{camera['id']}"
        seen.add(key)
        soon = camera["below_policy"] and camera["days_until_shortfall"] is not None and camera["days_until_shortfall"] <= WARN_DAYS
        sync_incident(db, incident_key=key, camera_id=camera["id"], fault_type="retention_forecast", summary=camera["message"] if soon else f"{camera['name']} keeps its retention policy", active=soon,
                      details={"policy_days": camera["policy_days"], "forecast_days": camera["forecast_days"], "days_until_shortfall": camera["days_until_shortfall"], "suggestions": camera["suggestions"]})
    for row in db.execute("SELECT incident_key,camera_id FROM incidents WHERE fault_type='retention_forecast' AND fault_active=1").fetchall():
        if row["incident_key"] not in seen:
            sync_incident(db, incident_key=row["incident_key"], camera_id=row["camera_id"], fault_type="retention_forecast", summary="Camera removed", active=False)
