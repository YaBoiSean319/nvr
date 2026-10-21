from __future__ import annotations

import os
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .recording import recordings_path
from .store import connect

DEFAULT_RETENTION_DAYS = 30
_lease_lock = threading.Lock()
_leased_paths: set[Path] = set()


def acquire_playback(path: Path) -> None:
    with _lease_lock:
        _leased_paths.add(path.resolve())


def release_playback(path: Path) -> None:
    with _lease_lock:
        _leased_paths.discard(path.resolve())


def playback_is_leased(path: Path) -> bool:
    with _lease_lock:
        return path.resolve() in _leased_paths


def unlink_if_unleased(path: Path) -> bool:
    """Atomically exclude cleanup while playback opens or reads a file."""
    with _lease_lock:
        if path.resolve() in _leased_paths:
            return False
        path.unlink(missing_ok=True)
        return True


def effective_retention(db, camera_id: str) -> tuple[int, str]:
    camera = db.execute("SELECT group_id FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if camera is None:
        return DEFAULT_RETENTION_DAYS, "default"
    override = db.execute("SELECT retention_days FROM retention_policy_overrides WHERE scope_type='camera' AND scope_id=?", (camera_id,)).fetchone()
    if override:
        return int(override["retention_days"]), "camera"
    if camera["group_id"]:
        override = db.execute("SELECT retention_days FROM retention_policy_overrides WHERE scope_type='group' AND scope_id=?", (camera["group_id"],)).fetchone()
        if override:
            return int(override["retention_days"]), "group"
    return DEFAULT_RETENTION_DAYS, "default"


def coverage_summary(db, camera_id: str, root: Path | None = None, now: datetime | None = None) -> dict[str, object]:
    root = (root or recordings_path()).resolve()
    now = now or datetime.now(timezone.utc)
    days, source = effective_retention(db, camera_id)
    rows = db.execute("SELECT starts_at,ends_at,size_bytes FROM recording_segments WHERE camera_id=? ORDER BY starts_at", (camera_id,)).fetchall()
    total_bytes = sum(max(0, int(row["size_bytes"])) for row in rows)
    def parse_time(value: str) -> datetime:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)

    spans = sorted((parse_time(row["starts_at"]), parse_time(row["ends_at"])) for row in rows)
    starts = [start for start, _ in spans]
    ends = [end for _, end in spans]
    merged: list[tuple[datetime, datetime]] = []
    for start, end in spans:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    actual_seconds = sum((end - start).total_seconds() for start, end in merged)
    oldest = min(starts).astimezone(timezone.utc).isoformat() if starts else None
    newest = max(ends).astimezone(timezone.utc).isoformat() if ends else None
    age_seconds = max(1.0, actual_seconds)
    bytes_per_day = total_bytes / age_seconds * 86400 if total_bytes else 0.0
    try:
        free_bytes = shutil.disk_usage(root).free
    except OSError:
        free_bytes = None
    projected_days = min(days, actual_seconds / 86400 + free_bytes / bytes_per_day) if free_bytes is not None and bytes_per_day > 0 else None
    return {
        "retention_days": days,
        "policy_source": source,
        "achieved_seconds": round(actual_seconds),
        "achieved_days": round(actual_seconds / 86400, 2),
        "oldest_footage_at": oldest,
        "newest_footage_at": newest,
        "indexed_bytes": total_bytes,
        "free_bytes": free_bytes,
        "projected_days_at_current_activity": round(projected_days, 2) if projected_days is not None else None,
        "projection_note": "Estimate uses observed indexed bitrate and current free filesystem capacity; camera activity and competing storage use can change it." if projected_days is not None else "Projection unavailable until indexed footage and filesystem capacity are measurable.",
        "as_of": now.astimezone(timezone.utc).isoformat(),
    }


def cleanup_expired_recordings(now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    root = recordings_path()
    removed_segments = removed_bytes = skipped_leased = failed = 0
    with connect() as db:
        rows = db.execute(
            "SELECT s.id,s.camera_id,s.relative_path,s.ends_at,s.size_bytes FROM recording_segments s ORDER BY s.ends_at"
        ).fetchall()
    for row in rows:
        try:
            end = datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
        except ValueError:
            failed += 1
            continue
        with connect() as db:
            days, _ = effective_retention(db, row["camera_id"])
        if end > now - timedelta(days=days):
            continue
        path = (root / row["relative_path"]).resolve()
        if root not in path.parents:
            failed += 1
            continue
        try:
            if not unlink_if_unleased(path):
                skipped_leased += 1
                continue
            with connect() as db:
                db.execute("DELETE FROM recording_segments WHERE id=?", (row["id"],))
            removed_segments += 1
            removed_bytes += int(row["size_bytes"])
        except OSError:
            failed += 1
    return {"removed_segments": removed_segments, "removed_bytes": removed_bytes, "skipped_playback": skipped_leased, "failed": failed}
