"""The Prometheus text exposition of the NVR's state, read when ``/metrics`` is scraped.

Labels carry camera ids and names, worker names and states, and never an address, stream URL or credential:
nothing here reads the columns that hold them, and a camera's name (free text) is scrubbed like a log line.
Each metric family is written whole, HELP and TYPE first, as the text format requires.
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


def _number(value: float | int) -> str:
    """Every digit of it: timestamps and byte counts must not be rounded to six figures."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # SQLite hands a counter back as REAL.
    return str(value) if isinstance(value, int) else repr(float(value))


class _Output:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def family(self, name: str, kind: str, help_text: str) -> None:
        self.lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]

    def sample(self, name: str, value: float | int | None, **labels: object) -> None:
        if value is None:
            return
        rendered = ",".join(f'{key}="{_escape(item)}"' for key, item in labels.items())
        self.lines.append(f"{name}{{{rendered}}} {_number(value)}" if rendered else f"{name} {_number(value)}")


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _cameras(out: _Output, db: sqlite3.Connection, scrub: logforward.Scrubber) -> None:
    cameras = [{"id": row["id"], "camera": scrub(row["name"]), "row": row} for row in db.execute(
        "SELECT id,name,enabled,recording_enabled,recording_status,last_footage_write_at FROM cameras WHERE archived_at IS NULL ORDER BY name,id")]
    counters = {(row["name"], row["camera_id"]): row["value"] for row in db.execute("SELECT name,camera_id,value FROM metric_counters")}

    def each(name: str, kind: str, help_text: str, value) -> None:
        out.family(name, kind, help_text)
        for camera in cameras:
            out.sample(name, value(camera), camera_id=camera["id"], camera=camera["camera"])

    out.family("nvr_camera_recording_state", "gauge", "1 for the state each camera's recording is in.")
    for camera in cameras:
        out.sample("nvr_camera_recording_state", 1, camera_id=camera["id"], camera=camera["camera"], state=camera["row"]["recording_status"])
    each("nvr_camera_recording_enabled", "gauge", "1 when recording is switched on for the camera.", lambda c: int(bool(c["row"]["enabled"] and c["row"]["recording_enabled"])))
    each("nvr_camera_last_footage_write_timestamp_seconds", "gauge", "When footage was last written for the camera.", lambda c: _timestamp(c["row"]["last_footage_write_at"]))
    tamper_states = {row["camera_id"]: row["state"] for row in db.execute("SELECT camera_id,state FROM tamper_settings WHERE enabled=1")}
    out.family("nvr_camera_tamper_state", "gauge", "1 for the state tamper detection reports for each camera that has it on: learning, ok, covered, moved or defocused.")
    for camera in cameras:
        if camera["id"] in tamper_states:
            out.sample("nvr_camera_tamper_state", 1, camera_id=camera["id"], camera=camera["camera"], state=tamper_states[camera["id"]])
    each("nvr_segments_written_total", "counter", "Segments that became playable.", lambda c: counters.get((SEGMENTS_WRITTEN, c["id"]), 0))
    each("nvr_segments_dropped_total", "counter",
         "Segments the recorder produced and could not keep: a file too small to be video, an unreadable index row, or a motion clip that could not be assembled.",
         lambda c: counters.get((SEGMENTS_DROPPED, c["id"]), 0))
    name = "nvr_segment_write_latency_seconds"
    out.family(name, "histogram", "Seconds from the end of a segment's footage until it was indexed and playable (the recorder looks every few seconds).")
    for camera in cameras:
        labels = {"camera_id": camera["id"], "camera": camera["camera"]}
        written = counters.get((SEGMENTS_WRITTEN, camera["id"]), 0)
        for bound in LATENCY_BUCKETS:
            out.sample(f"{name}_bucket", counters.get((f"{LATENCY_BUCKET}:{bound}", camera["id"]), 0), le=bound, **labels)
        out.sample(f"{name}_bucket", written, le="+Inf", **labels)
        out.sample(f"{name}_sum", counters.get((LATENCY_SUM, camera["id"]), 0), **labels)
        out.sample(f"{name}_count", written, **labels)


def _live(out: _Output) -> None:
    from .api.live_media import live_counts  # The streams live in the web process, which serves this.
    from .viewing import MAX_LIVE_AUDIO_LISTENERS, MAX_LIVE_VIEWERS
    viewers, listeners = live_counts()
    for name, help_text, value in (
        ("nvr_live_viewers", "Live video streams being delivered.", viewers),
        ("nvr_live_viewer_slots", "Live video streams the NVR allows at once.", MAX_LIVE_VIEWERS),
        ("nvr_live_audio_listeners", "Live audio listeners.", listeners),
        ("nvr_live_audio_listener_slots", "Live audio listeners allowed at once.", MAX_LIVE_AUDIO_LISTENERS),
    ):
        out.family(name, "gauge", help_text)
        out.sample(name, value)


def _workers(out: _Output, supervised: bool) -> None:
    """Without the supervisor (one process, as in tests) every worker is reported up and never restarted."""
    workers = liveness.report(supervised)
    out.family("nvr_worker_up", "gauge", "1 when the worker has reported alive recently.")
    for worker in workers:
        out.sample("nvr_worker_up", int(bool(worker["alive"])), worker=worker["name"])
    out.family("nvr_worker_restarts_total", "counter", "Times the supervisor restarted the worker.")
    for worker in workers:
        out.sample("nvr_worker_restarts_total", worker.get("restarts", 0), worker=worker["name"])
    out.family("nvr_worker_heartbeat_timestamp_seconds", "gauge", "The worker's last heartbeat.")
    for worker in workers:
        out.sample("nvr_worker_heartbeat_timestamp_seconds", _timestamp(worker.get("heartbeat_at")), worker=worker["name"])


def _exports(out: _Output, db: sqlite3.Connection) -> None:
    counts = {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) AS n FROM exports GROUP BY status")}
    out.family("nvr_export_jobs", "gauge", "Export jobs by state.")
    for state in ("queued", "running", "completed", "failed", "cancelled"):
        out.sample("nvr_export_jobs", counts.get(state, 0), state=state)
    oldest = _timestamp(db.execute("SELECT MIN(created_at) AS at FROM exports WHERE status='queued'").fetchone()["at"])
    out.family("nvr_export_oldest_queued_age_seconds", "gauge", "How long the oldest queued export has waited.")
    out.sample("nvr_export_oldest_queued_age_seconds", max(0, datetime.now(timezone.utc).timestamp() - oldest) if oldest else 0)


def _ai(out: _Output, db: sqlite3.Connection, scrub: logforward.Scrubber) -> None:
    rows = db.execute("SELECT s.camera_id,c.name,s.status,s.last_success_at FROM ai_camera_settings s JOIN cameras c ON c.id=s.camera_id "
                      "WHERE s.enabled=1 AND c.archived_at IS NULL ORDER BY c.name,c.id").fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    out.family("nvr_ai_cameras", "gauge", "Cameras with analysis on, by state.")
    for state, count in sorted(counts.items()):
        out.sample("nvr_ai_cameras", count, state=state)
    out.family("nvr_ai_backlog_cameras", "gauge", "Cameras whose analysis is delayed or failing behind real time.")
    out.sample("nvr_ai_backlog_cameras", counts.get("delayed", 0) + counts.get("failed", 0))
    out.family("nvr_ai_seconds_since_last_analysis", "gauge", "Seconds since the camera's last analysed frame.")
    now = datetime.now(timezone.utc).timestamp()
    for row in rows:
        at = _timestamp(row["last_success_at"])
        out.sample("nvr_ai_seconds_since_last_analysis", None if at is None else max(0, now - at), camera_id=row["camera_id"], camera=scrub(row["name"]))


def _storage(out: _Output) -> None:
    """Reading volume health writes and removes a small probe file on each volume, which is also how the write latency is measured."""
    volumes = storage.storage_status()["volumes"]
    for name, kind, help_text, value in (
        ("nvr_storage_free_bytes", "gauge", "Free space on each recording volume.", lambda v: v["free_bytes"]),
        ("nvr_storage_capacity_bytes", "gauge", "Capacity of each recording volume.", lambda v: v["capacity_bytes"]),
        ("nvr_storage_write_latency_seconds", "gauge", "How long a small write to the volume took.", lambda v: None if v["write_latency_ms"] is None else v["write_latency_ms"] / 1000),
    ):
        out.family(name, kind, help_text)
        for volume in volumes:
            out.sample(name, value(volume), volume=volume["id"])
    out.family("nvr_storage_volume_state", "gauge", "1 for the state each recording volume is in.")
    for volume in volumes:
        out.sample("nvr_storage_volume_state", 1, volume=volume["id"], state=volume["state"])


def _disks(out: _Output, db: sqlite3.Connection, scrub: logforward.Scrubber) -> None:
    from . import forecast
    rows = db.execute("SELECT volume_id,state FROM disk_health ORDER BY volume_id").fetchall()
    out.family("nvr_disk_health_state", "gauge", "1 for the SMART state of the disk behind each recording volume: healthy, warning, failing, or unavailable when it cannot be read.")
    for row in rows:
        out.sample("nvr_disk_health_state", 1, volume=row["volume_id"], state=row["state"])
    try:
        result = forecast.forecast(db)
    except (sqlite3.Error, ValueError, OSError):
        return  # The forecast families are skipped; the rest of the scrape stands.
    for name, help_text, key in (("nvr_camera_retention_policy_days", "The retention policy of each camera, in days.", "policy_days"),
                                 ("nvr_camera_retention_forecast_days", "How many days of footage each camera is forecast to keep at its recent write rate.", "forecast_days")):
        out.family(name, "gauge", help_text)
        for camera in result["cameras"]:
            if camera[key] is not None:
                out.sample(name, camera[key], camera_id=camera["id"], camera=scrub(camera["name"]))


def _backup(out: _Output) -> None:
    status = backup.status()
    out.family("nvr_backup_enabled", "gauge", "1 when configuration backups are configured.")
    out.sample("nvr_backup_enabled", int(bool(status["enabled"])))
    out.family("nvr_backup_age_seconds", "gauge", "Seconds since the last successful configuration backup.")
    if status["age_hours"] is not None:
        out.sample("nvr_backup_age_seconds", status["age_hours"] * 3600)
    out.family("nvr_backup_stale", "gauge", "1 when the last backup is older than it should be.")
    out.sample("nvr_backup_stale", int(bool(status["stale"])))


def render(supervised: bool) -> str:
    """The Prometheus text exposition (format 0.0.4) of the NVR's state now."""
    out = _Output()
    out.family("nvr_build_info", "gauge", "The NVR version.")
    out.sample("nvr_build_info", 1, version=APP_VERSION)
    scrub = logforward.Scrubber()
    try:
        scrub.refresh()
    except sqlite3.Error:
        pass  # The URL pattern alone then.
    with connect() as db:
        _cameras(out, db, scrub)
        _exports(out, db)
        _ai(out, db, scrub)
    _live(out)
    _workers(out, supervised)
    _storage(out)
    with connect() as db:
        _disks(out, db, scrub)
    _backup(out)
    logforward.describe(out)
    return "\n".join(out.lines) + "\n"
