"""The Prometheus text exposition of the NVR's state, read when ``/metrics`` is scraped.

Labels carry camera ids and names, worker names and states, and never an address, stream URL or credential:
nothing here reads the columns that hold them.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import backup, logforward, storage
from .metrics import LATENCY_BUCKET, LATENCY_BUCKETS, LATENCY_SUM, SEGMENTS_DROPPED, SEGMENTS_WRITTEN
from .store import connect
from .version import APP_VERSION
from .workers import liveness


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class _Output:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def declare(self, name: str, kind: str, help_text: str) -> None:
        if name not in self._declared:
            self._declared.add(name)
            self.lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]

    def sample(self, name: str, value: float | int | None, **labels: object) -> None:
        if value is None:
            return
        rendered = ",".join(f'{key}="{_escape(item)}"' for key, item in labels.items())
        self.lines.append(f"{name}{{{rendered}}} {value:g}" if rendered else f"{name} {value:g}")


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _cameras(out: _Output, db: sqlite3.Connection, counters: dict[tuple[str, str], float]) -> None:
    rows = db.execute("SELECT id,name,enabled,recording_enabled,recording_status,last_footage_write_at,archived_at FROM cameras WHERE archived_at IS NULL ORDER BY name,id").fetchall()
    out.declare("nvr_camera_recording_state", "gauge", "1 for the state each camera's recording is in.")
    out.declare("nvr_camera_recording_enabled", "gauge", "1 when recording is switched on for the camera.")
    out.declare("nvr_camera_last_footage_write_timestamp_seconds", "gauge", "When footage was last written for the camera.")
    for row in rows:
        labels = {"camera_id": row["id"], "camera": row["name"]}
        out.sample("nvr_camera_recording_state", 1, state=row["recording_status"], **labels)
        out.sample("nvr_camera_recording_enabled", int(bool(row["enabled"] and row["recording_enabled"])), **labels)
        out.sample("nvr_camera_last_footage_write_timestamp_seconds", _timestamp(row["last_footage_write_at"]), **labels)
    out.declare("nvr_segments_written_total", "counter", "Segments that became playable.")
    out.declare("nvr_segments_dropped_total", "counter", "Segments the recorder produced and could not keep.")
    out.declare("nvr_segment_write_latency_seconds", "histogram", "Seconds from the end of a segment's footage until it was indexed and playable.")
    for row in rows:
        camera_id = row["id"]
        labels = {"camera_id": camera_id, "camera": row["name"]}
        written = counters.get((SEGMENTS_WRITTEN, camera_id), 0)
        out.sample("nvr_segments_written_total", written, **labels)
        out.sample("nvr_segments_dropped_total", counters.get((SEGMENTS_DROPPED, camera_id), 0), **labels)
        for bound in LATENCY_BUCKETS:
            out.sample("nvr_segment_write_latency_seconds_bucket", counters.get((f"{LATENCY_BUCKET}:{bound}", camera_id), 0), le=bound, **labels)
        out.sample("nvr_segment_write_latency_seconds_bucket", written, le="+Inf", **labels)
        out.sample("nvr_segment_write_latency_seconds_sum", counters.get((LATENCY_SUM, camera_id), 0), **labels)
        out.sample("nvr_segment_write_latency_seconds_count", written, **labels)


def _live(out: _Output) -> None:
    from .api import live_media  # The counters live with the streams, in the web process that serves this.
    from .viewing import MAX_LIVE_AUDIO_LISTENERS, MAX_LIVE_VIEWERS
    out.declare("nvr_live_viewers", "gauge", "Live video streams being delivered.")
    out.declare("nvr_live_viewer_slots", "gauge", "Live video streams the NVR allows at once.")
    out.declare("nvr_live_audio_listeners", "gauge", "Live audio listeners.")
    out.declare("nvr_live_audio_listener_slots", "gauge", "Live audio listeners allowed at once.")
    with live_media._live_view_lock:
        viewers, listeners = live_media._live_view_count, live_media._live_audio_count
    out.sample("nvr_live_viewers", viewers)
    out.sample("nvr_live_viewer_slots", MAX_LIVE_VIEWERS)
    out.sample("nvr_live_audio_listeners", listeners)
    out.sample("nvr_live_audio_listener_slots", MAX_LIVE_AUDIO_LISTENERS)


def _workers(out: _Output, supervised: bool) -> None:
    out.declare("nvr_worker_up", "gauge", "1 when the worker has reported alive recently.")
    out.declare("nvr_worker_restarts_total", "counter", "Times the supervisor restarted the worker.")
    out.declare("nvr_worker_heartbeat_timestamp_seconds", "gauge", "The worker's last heartbeat.")
    for worker in liveness.report(supervised):
        out.sample("nvr_worker_up", int(bool(worker["alive"])), worker=worker["name"])
        out.sample("nvr_worker_restarts_total", worker.get("restarts", 0), worker=worker["name"])
        out.sample("nvr_worker_heartbeat_timestamp_seconds", _timestamp(worker.get("heartbeat_at")), worker=worker["name"])


def _exports(out: _Output, db: sqlite3.Connection) -> None:
    out.declare("nvr_export_jobs", "gauge", "Export jobs by state.")
    out.declare("nvr_export_oldest_queued_age_seconds", "gauge", "How long the oldest queued export has waited.")
    counts = {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) AS n FROM exports GROUP BY status")}
    for state in ("queued", "running", "completed", "failed", "cancelled"):
        out.sample("nvr_export_jobs", counts.get(state, 0), state=state)
    oldest = db.execute("SELECT MIN(created_at) AS at FROM exports WHERE status='queued'").fetchone()["at"]
    age = (datetime.now(timezone.utc).timestamp() - _timestamp(oldest)) if _timestamp(oldest) else 0
    out.sample("nvr_export_oldest_queued_age_seconds", max(0, age))


def _ai(out: _Output, db: sqlite3.Connection) -> None:
    out.declare("nvr_ai_cameras", "gauge", "Cameras with analysis on, by state.")
    out.declare("nvr_ai_backlog_cameras", "gauge", "Cameras whose analysis is delayed or failing behind real time.")
    out.declare("nvr_ai_seconds_since_last_analysis", "gauge", "Seconds since the camera's last analysed frame.")
    rows = db.execute("SELECT s.camera_id,c.name,s.status,s.last_success_at FROM ai_camera_settings s JOIN cameras c ON c.id=s.camera_id WHERE s.enabled=1 AND c.archived_at IS NULL").fetchall()
    counts: dict[str, int] = {}
    now = datetime.now(timezone.utc).timestamp()
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        at = _timestamp(row["last_success_at"])
        out.sample("nvr_ai_seconds_since_last_analysis", None if at is None else max(0, now - at), camera_id=row["camera_id"], camera=row["name"])
    for state, count in sorted(counts.items()):
        out.sample("nvr_ai_cameras", count, state=state)
    out.sample("nvr_ai_backlog_cameras", counts.get("delayed", 0) + counts.get("failed", 0))


def _storage(out: _Output) -> None:
    out.declare("nvr_storage_free_bytes", "gauge", "Free space on each recording volume.")
    out.declare("nvr_storage_capacity_bytes", "gauge", "Capacity of each recording volume.")
    out.declare("nvr_storage_volume_state", "gauge", "1 for the state each recording volume is in.")
    for volume in storage.storage_status()["volumes"]:
        out.sample("nvr_storage_free_bytes", volume["free_bytes"], volume=volume["id"])
        out.sample("nvr_storage_capacity_bytes", volume["capacity_bytes"], volume=volume["id"])
        out.sample("nvr_storage_volume_state", 1, volume=volume["id"], state=volume["state"])


def _backup(out: _Output) -> None:
    out.declare("nvr_backup_enabled", "gauge", "1 when configuration backups are configured.")
    out.declare("nvr_backup_age_seconds", "gauge", "Seconds since the last successful configuration backup.")
    out.declare("nvr_backup_stale", "gauge", "1 when the last backup is older than it should be.")
    status = backup.status()
    out.sample("nvr_backup_enabled", int(bool(status["enabled"])))
    if status["age_hours"] is not None:
        out.sample("nvr_backup_age_seconds", status["age_hours"] * 3600)
    out.sample("nvr_backup_stale", int(bool(status["stale"])))


def render(supervised: bool) -> str:
    """The Prometheus text exposition (format 0.0.4) of the NVR's state now."""
    out = _Output()
    out.declare("nvr_build_info", "gauge", "The NVR version.")
    out.sample("nvr_build_info", 1, version=APP_VERSION)
    with connect() as db:
        counters = {(row["name"], row["camera_id"]): row["value"] for row in db.execute("SELECT name,camera_id,value FROM metric_counters")}
        _cameras(out, db, counters)
        _exports(out, db)
        _ai(out, db)
    _live(out)
    _workers(out, supervised)
    _storage(out)
    _backup(out)
    logforward.describe(out)
    return "\n".join(out.lines) + "\n"
