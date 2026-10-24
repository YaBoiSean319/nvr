from __future__ import annotations

import fcntl
import os
import sqlite3
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .alarms import protects_footage, resolved_kept_since
from .recording import recordings_path
from .store import connect

DEFAULT_RETENTION_DAYS = 30
_lease_lock = threading.Lock()
# Each lease holds its own open file description with a shared flock on the file.
# Locks are visible to every process on the host and vanish if the holder dies, so
# cleanup in the recorder process respects playback and downloads in the web process.
_leases: dict[Path, list[int]] = {}
_NO_FILE = -1


def acquire_playback(path: Path) -> None:
    resolved = path.resolve()
    descriptor = _shared_lock(resolved)
    with _lease_lock:
        _leases.setdefault(resolved, []).append(descriptor)


def release_playback(path: Path) -> None:
    resolved = path.resolve()
    with _lease_lock:
        descriptors = _leases.get(resolved)
        if not descriptors:
            return
        descriptor = descriptors.pop()
        if not descriptors:
            _leases.pop(resolved, None)
    if descriptor != _NO_FILE:
        os.close(descriptor)


def playback_is_leased(path: Path) -> bool:
    """Whether any process currently holds a lease on the file."""
    descriptors = _exclusive_locks([path.resolve()])
    if descriptors is None:
        return True
    _close(descriptors)
    return False


def unlink_if_unleased(path: Path) -> bool:
    """Delete a file only while no process holds a lease on it."""
    return unlink_many_if_unleased([path])


def unlink_many_if_unleased(paths: list[Path]) -> bool:
    """Delete files only when none of them is currently leased by any process."""
    resolved = list(dict.fromkeys(path.resolve() for path in paths))
    descriptors = _exclusive_locks(resolved)
    if descriptors is None:
        return False
    try:
        for path in resolved:
            path.unlink(missing_ok=True)
    finally:
        _close(descriptors)
    return True


def _shared_lock(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError):
        return _NO_FILE
    # Waits only while a deletion holds its brief exclusive lock.
    fcntl.flock(descriptor, fcntl.LOCK_SH)
    try:
        current = os.stat(path)
    except FileNotFoundError:
        current = None
    if current is None or current.st_ino != os.fstat(descriptor).st_ino:
        # Deleted (or replaced) while we waited: there is nothing left to protect.
        os.close(descriptor)
        return _NO_FILE
    return descriptor


def _exclusive_locks(paths: list[Path]) -> list[int] | None:
    """Exclusively lock every existing file, or return None if any is leased."""
    held: list[int] = []
    for path in paths:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except (FileNotFoundError, NotADirectoryError):
            continue
        held.append(descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _close(held)
            return None
    return held


def _close(descriptors: list[int]) -> None:
    for descriptor in descriptors:
        os.close(descriptor)


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
    rows = db.execute("SELECT starts_at,ends_at,size_bytes FROM recording_segments WHERE camera_id=? AND unavailable_at IS NULL ORDER BY starts_at", (camera_id,)).fetchall()
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
        with connect() as connection:
            group = connection.execute("SELECT g.storage_volume_id FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?",(camera_id,)).fetchone()
        if group and group["storage_volume_id"]:
            from .storage import volume_path
            capacity_path=volume_path(group["storage_volume_id"])
        else:
            capacity_path=root
        free_bytes = shutil.disk_usage(capacity_path).free
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
    removed_segments = removed_bytes = skipped_leased = skipped_alarm = failed = 0
    with connect() as db:
        kept_since = resolved_kept_since(db, now)
        expired=db.execute("SELECT id,segment_id FROM recording_holds WHERE released_at IS NULL AND expires_at IS NOT NULL AND expires_at<=?",(now.astimezone(timezone.utc).isoformat(),)).fetchall()
        for hold in expired:
            db.execute("UPDATE recording_holds SET released_at=expires_at WHERE id=?",(hold["id"],))
            db.execute("INSERT INTO audit_log(action,resource_type,resource_id,details_json) VALUES('expire_hold','recording_segment',?,'{}')",(str(hold["segment_id"] or "unknown"),))
        rows = db.execute("SELECT s.id,s.camera_id,s.relative_path,s.ends_at,s.size_bytes FROM recording_segments s ORDER BY s.ends_at").fetchall()
    for row in rows:
        try:
            end = datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
        except ValueError:
            failed += 1
            continue
        try:
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current=db.execute("SELECT id,camera_id,relative_path,starts_at,ends_at,size_bytes FROM recording_segments WHERE id=?",(row["id"],)).fetchone()
                if current is None:
                    continue
                active=db.execute("SELECT 1 FROM recording_holds WHERE segment_id=? AND released_at IS NULL AND (expires_at IS NULL OR expires_at>?) LIMIT 1",(row["id"],now.astimezone(timezone.utc).isoformat())).fetchone()
                if active:
                    continue
                if protects_footage(db,current["camera_id"],current["starts_at"],current["ends_at"],kept_since):
                    skipped_alarm+=1
                    continue
                reserved=db.execute(
                    "SELECT 1 FROM export_sources s JOIN exports e ON e.id=s.export_id "
                    "WHERE s.segment_id=? AND e.status IN ('queued','running') LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if reserved:
                    skipped_leased+=1
                    continue
                days,_=effective_retention(db,row["camera_id"])
                current_end=datetime.fromisoformat(current["ends_at"].replace("Z","+00:00"))
                if current_end.tzinfo is None: current_end=current_end.replace(tzinfo=timezone.utc)
                if current_end>now-timedelta(days=days):
                    continue
                from .storage import recording_file_path
                path=recording_file_path(current["relative_path"])
                if not unlink_if_unleased(path):
                    skipped_leased+=1
                    continue
                db.execute("DELETE FROM recording_segments WHERE id=?", (row["id"],))
            removed_segments += 1
            removed_bytes += int(current["size_bytes"])
        except (OSError,sqlite3.Error):
            failed += 1
    # Deleting footage deletes its detections; detections no footage covers expire on the same schedule.
    from .tracks import purge_expired_detections
    with connect() as db:
        cameras=[row[0] for row in db.execute("SELECT DISTINCT camera_id FROM ai_tracks")]
    for camera_id in cameras:
        try:
            with connect() as db:
                days,_=effective_retention(db,camera_id)
                purge_expired_detections(db,camera_id,now-timedelta(days=days))
        except sqlite3.Error:
            failed += 1
    return {"removed_segments": removed_segments, "removed_bytes": removed_bytes, "skipped_playback": skipped_leased,
            "kept_for_alarms": skipped_alarm, "failed": failed}
