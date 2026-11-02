"""Storage headroom (#219): storage space comes before retention policy.

A recorder fills its disk by design, so each storage volume keeps a **headroom** of free space. When free space is below it, the oldest footage on the volume is deleted, whatever its
retention policy says, until the headroom is free again. Footage that is held, reserved by an export, covered by an alarm or being played is never deleted; if that alone fills the
volume the shortfall is reported beside the setting instead.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

from .store import connect

GIB = 1 << 30
DEFAULT_SHARE = 0.05
DEFAULT_FLOOR_BYTES = 5 * GIB
MAX_SEGMENTS_PER_PASS = 5000
RECENT_SECONDS = 600  # Footage removed within this long means the volume is "keeping its headroom now"


def default_headroom(capacity_bytes: int | None) -> int:
    """5% of the volume, and at least 5 GiB, so a site that never sets it still keeps room for exports and for the recorder."""
    return max(DEFAULT_FLOOR_BYTES, int((capacity_bytes or 0) * DEFAULT_SHARE))


def chosen_headroom(db: sqlite3.Connection, volume_id: str) -> int | None:
    row = db.execute("SELECT headroom_bytes FROM storage_headroom WHERE volume_id=?", (volume_id,)).fetchone()
    return None if row is None or row["headroom_bytes"] is None else int(row["headroom_bytes"])


def effective_headroom(db: sqlite3.Connection, volume_id: str, capacity_bytes: int | None) -> int:
    chosen = chosen_headroom(db, volume_id)
    return default_headroom(capacity_bytes) if chosen is None else chosen


def set_headroom(volume_id: str, headroom_bytes: int | None) -> None:
    """Choose a volume's headroom (None: the default again)."""
    with connect() as db:
        db.execute(
            "INSERT INTO storage_headroom(volume_id,headroom_bytes) VALUES(?,?) "
            "ON CONFLICT(volume_id) DO UPDATE SET headroom_bytes=excluded.headroom_bytes,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (volume_id, headroom_bytes))


def volume_of(relative_path: str, volumes: dict[str, str]) -> str:
    """The storage volume a segment's path is under: the longest configured volume path that prefixes it, else the default volume."""
    best, name = -1, "default"
    for volume_id, relative in volumes.items():
        prefix = relative.strip("/")
        if (not prefix or relative_path == prefix or relative_path.startswith(prefix + "/")) and len(prefix) > best:
            best, name = len(prefix), volume_id
    return name


def _record_check(volume_id: str, now: datetime, short_bytes: int) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO storage_headroom(volume_id,last_checked_at,short_bytes) VALUES(?,?,?) "
            "ON CONFLICT(volume_id) DO UPDATE SET last_checked_at=excluded.last_checked_at,short_bytes=excluded.short_bytes",
            (volume_id, now.isoformat(), short_bytes))


def keep_headroom(now: datetime | None = None) -> dict[str, dict[str, int]]:
    """One pass over every available volume: delete the oldest removable footage until each has its headroom free (or nothing removable is left)."""
    from .alarms import resolved_kept_since
    from .retention import remove_segment
    from .storage import configured_volumes, volume_path
    from .tiering import discard_objects

    now = now or datetime.now(timezone.utc)
    volumes = configured_volumes()
    results: dict[str, dict[str, int]] = {}
    for volume_id in volumes:
        try:
            path = volume_path(volume_id)
            usage = shutil.disk_usage(path)
        except (OSError, ValueError):
            continue
        with connect() as db:
            wanted = effective_headroom(db, volume_id, usage.total)
        free = usage.free
        removed_segments = removed_bytes = 0
        newest = None
        if free < wanted:
            with connect() as db:
                kept_since = resolved_kept_since(db, now)
                candidates = db.execute("SELECT id,relative_path,ends_at FROM recording_segments WHERE tier_id IS NULL ORDER BY ends_at").fetchall()
            examined = 0
            for candidate in candidates:
                if free >= wanted or examined >= MAX_SEGMENTS_PER_PASS:
                    break
                if volume_of(candidate["relative_path"], volumes) != volume_id:
                    continue
                examined += 1
                try:
                    with connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        outcome, current = remove_segment(db, candidate["id"], now, kept_since, policy_applies=False)
                except (OSError, sqlite3.Error):
                    continue
                if outcome != "removed":
                    continue
                discard_objects([current])
                removed_segments += 1
                removed_bytes += int(current["size_bytes"])
                newest = max(newest or current["ends_at"], current["ends_at"])
                free += int(current["size_bytes"])
                if removed_segments % 20 == 0:  # The files' sizes are close to what was freed; re-read the disk now and then
                    free = shutil.disk_usage(path).free
            free = shutil.disk_usage(path).free
        if removed_segments:
            with connect() as db:
                db.execute("INSERT INTO storage_headroom_removals(volume_id,removed_at,segments,bytes,newest_removed_at) VALUES(?,?,?,?,?)",
                           (volume_id, now.isoformat(), removed_segments, removed_bytes, newest))
        _record_check(volume_id, now, max(0, wanted - free))
        results[volume_id] = {"removed_segments": removed_segments, "removed_bytes": removed_bytes, "short_bytes": max(0, wanted - free)}
    return results


def status(db: sqlite3.Connection, volume_id: str, capacity_bytes: int | None, free_bytes: int | None, volumes: dict[str, str], now: datetime | None = None) -> dict[str, object]:
    """What the settings page shows beside a volume's headroom setting."""
    from .retention import effective_retention

    now = now or datetime.now(timezone.utc)
    wanted = effective_headroom(db, volume_id, capacity_bytes)
    row = db.execute("SELECT last_checked_at,short_bytes FROM storage_headroom WHERE volume_id=?", (volume_id,)).fetchone()
    day_ago = (now - timedelta(hours=24)).isoformat()
    recent = (now - timedelta(seconds=RECENT_SECONDS)).isoformat()
    totals = db.execute("SELECT COALESCE(SUM(segments),0) AS segments,COALESCE(SUM(bytes),0) AS bytes FROM storage_headroom_removals WHERE volume_id=? AND removed_at>=?", (volume_id, day_ago)).fetchone()
    removing = db.execute("SELECT 1 FROM storage_headroom_removals WHERE volume_id=? AND removed_at>=? LIMIT 1", (volume_id, recent)).fetchone() is not None
    oldest = None
    days_policy = 0
    cameras = db.execute("SELECT c.id,g.storage_volume_id FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id").fetchall()
    on_volume = [camera["id"] for camera in cameras if (camera["storage_volume_id"] or "default") == volume_id]
    for camera_id in on_volume:
        days_policy = max(days_policy, effective_retention(db, camera_id)[0])
    if on_volume:
        marks = ",".join("?" for _ in on_volume)
        first = db.execute(f"SELECT MIN(starts_at) AS first FROM recording_segments WHERE camera_id IN ({marks}) AND tier_id IS NULL", on_volume).fetchone()
        oldest = first["first"] if first else None
    kept_days = None
    if oldest:
        try:
            started = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
            kept_days = round((now - (started if started.tzinfo else started.replace(tzinfo=timezone.utc))).total_seconds() / 86400, 1)
        except ValueError:
            kept_days = None
    return {
        "headroom_bytes": wanted,
        "headroom_is_default": chosen_headroom(db, volume_id) is None,
        "headroom_default_bytes": default_headroom(capacity_bytes),
        "below_headroom": free_bytes is not None and free_bytes < wanted,
        "removing_oldest_footage": removing,
        "removed_24h_segments": int(totals["segments"]),
        "removed_24h_bytes": int(totals["bytes"]),
        "short_bytes": int(row["short_bytes"]) if row else 0,
        "last_checked_at": row["last_checked_at"] if row else None,
        "oldest_footage_days": kept_days,
        "longest_policy_days": days_policy or None,
    }
