"""The nightly self-test, and the weekly coverage report's schedule.

Each night the self-test checks that every camera expected to record wrote footage in the
last day, every recording volume is writable, and the latest configuration backup is
fresh. Each failed check raises its own incident, which recovers when a later run passes;
an administrator can run it at any time to confirm a fix.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any

from .incidents import sync_incident
from .store import connect

logger = logging.getLogger(__name__)

SCHEDULE_TIME = time(3, 41)  # After the nightly backup at 02:17, so its result is fresh.
SILENT_AFTER = timedelta(days=1)
FAULT_TYPE = "self_test"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _result(check: str, kind: str, name: str, passed: bool | None, detail: str, **extra: Any) -> dict[str, Any]:
    """One check's outcome. ``passed`` is None when the check does not apply (it neither passes nor fails)."""
    return {"check": check, "kind": kind, "name": name, "passed": passed, "detail": detail, **extra}


def _check_cameras(now: datetime) -> list[dict[str, Any]]:
    """Cameras that were expected to record at some point in the last day, and whether they wrote footage."""
    since = (now - SILENT_AFTER).isoformat()
    with connect() as db:
        rows = db.execute(
            "SELECT c.id, c.name, (SELECT MAX(ends_at) FROM recording_segments s WHERE s.camera_id=c.id) AS last_footage "
            "FROM cameras c WHERE c.archived_at IS NULL AND c.enabled=1 AND c.recording_enabled=1 AND EXISTS ("
            "SELECT 1 FROM coverage_intervals i WHERE i.camera_id=c.id AND i.coverage_kind!='expected_idle' AND (i.ends_at IS NULL OR i.ends_at>?)) "
            "ORDER BY c.name COLLATE NOCASE, c.id", (since,)).fetchall()
    results = []
    for row in rows:
        passed = row["last_footage"] is not None and row["last_footage"] > since
        results.append(_result(f"camera:{row['id']}", "camera", row["name"], passed,
                               f"{row['name']}: last footage {row['last_footage']}" if passed else
                               f"{row['name']} has written no footage in the last day" + (f" (last footage {row['last_footage']})" if row["last_footage"] else ""),
                               camera_id=row["id"]))
    return results


def _check_volumes() -> list[dict[str, Any]]:
    from .storage import storage_status

    return [_result(f"volume:{volume['id']}", "volume", volume["id"], bool(volume["writable"]),
                    f"Recording volume {volume['id']} is writable" if volume["writable"] else f"Recording volume {volume['id']} is not writable ({volume['state']})")
            for volume in storage_status()["volumes"]]


def _check_backup() -> list[dict[str, Any]]:
    from . import backup

    state = backup.status()
    if not state["enabled"]:
        return [_result("backup", "backup", "Configuration backup", None, f"Configuration backups are not configured: {state['disabled_reason']}")]
    if state["age_hours"] is None:
        # A new installation has not reached its first nightly backup yet: only too long without one is a failure.
        with connect() as db:
            first = db.execute("SELECT MIN(created_at) FROM backup_runs").fetchone()[0]
        overdue = first is not None and _now() - datetime.fromisoformat(first.replace("Z", "+00:00")) > backup.STALE_AFTER
        return [_result("backup", "backup", "Configuration backup", False if overdue else None,
                        "No configuration backup has succeeded yet" + ("" if overdue else "; the first one runs tonight"))]
    if state["stale"]:
        return [_result("backup", "backup", "Configuration backup", False, f"The latest configuration backup is {state['age_hours']:.0f} hours old")]
    return [_result("backup", "backup", "Configuration backup", True, f"The latest configuration backup is {state['age_hours']:.1f} hours old")]


def run(kind: str = "manual") -> dict[str, Any]:
    """Run every check, record the run, and raise or recover an incident for each check."""
    started = _now()
    with connect() as db:
        run_id = db.execute("INSERT INTO self_test_runs(kind,started_at) VALUES(?,?)", (kind, started.isoformat())).lastrowid
    try:
        results = _check_cameras(started) + _check_volumes() + _check_backup()
    except BaseException:
        with connect() as db:  # A run that never finished must not count as tonight's, or it would not be retried.
            db.execute("DELETE FROM self_test_runs WHERE id=?", (run_id,))
        raise
    passed = all(result["passed"] is not False for result in results)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            observed = set()
            for result in results:
                key = f"{FAULT_TYPE}:{result['check']}"
                observed.add(key)
                if result["passed"] is None:
                    continue
                sync_incident(db, incident_key=key, camera_id=result.get("camera_id"), fault_type=FAULT_TYPE,
                              summary=f"Self-test failed: {result['detail']}", active=not result["passed"], details={"check": result["check"], "detail": result["detail"]})
            # A check that no longer exists (a removed camera or volume) has nothing left to fail.
            for row in db.execute("SELECT incident_key FROM incidents WHERE fault_type=? AND fault_active=1", (FAULT_TYPE,)).fetchall():
                if row["incident_key"] not in observed:
                    sync_incident(db, incident_key=row["incident_key"], camera_id=None, fault_type=FAULT_TYPE,
                                  summary="Self-test check no longer applies", active=False, details={})
            db.execute("UPDATE self_test_runs SET finished_at=?,passed=?,results_json=? WHERE id=?",
                       (_now().isoformat(), int(passed), json.dumps(results), run_id))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return latest()  # type: ignore[return-value]


def latest() -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM self_test_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return {"id": row["id"], "kind": row["kind"], "started_at": row["started_at"], "finished_at": row["finished_at"],
            "passed": bool(row["passed"]), "results": json.loads(row["results_json"])}


def due(now: datetime | None = None) -> bool:
    """Whether tonight's scheduled run has not started yet."""
    from .backup import site_zone

    now = now or _now()
    zone = site_zone()
    local = now.astimezone(zone)
    boundary = datetime.combine(local.date(), SCHEDULE_TIME, zone)
    if local < boundary:
        boundary -= timedelta(days=1)
    with connect() as db:
        return db.execute("SELECT 1 FROM self_test_runs WHERE kind='scheduled' AND finished_at IS NOT NULL AND started_at>=? LIMIT 1",
                          (boundary.astimezone(timezone.utc).isoformat(),)).fetchone() is None


class SelfTestManager:
    """Runs the nightly self-test and publishes the weekly coverage report, in the background worker."""

    def __init__(self, poll_seconds: float = 60.0) -> None:
        self.poll_seconds = poll_seconds
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="self-test")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    @staticmethod
    def process_once() -> None:
        from .coverage import publish_weekly_report

        if due():
            run("scheduled")
        publish_weekly_report()

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.process_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - tried again next poll
                logger.exception("Self-test cycle failed")
            await asyncio.sleep(self.poll_seconds)
