"""Start, watch and restart the NVR worker processes.

Each worker runs as its own process so a failure in one (for example the web
process) does not interrupt the others (for example recording). A worker that
exits or stops reporting liveness is restarted with exponential backoff, and an
incident stays active until it is running and reporting again.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app import backup, migrations
from app.detection import mark_analysis_interrupted
from app.incidents import sync_incident
from app.store import connect, initialize
from app.workers import liveness

logger = logging.getLogger("nvr.supervisor")

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
STABLE_AFTER_SECONDS = 60.0
POLL_SECONDS = 0.5
STOP_GRACE_SECONDS = float(os.environ.get("NVR_STOP_GRACE_SECONDS", "25"))

SUMMARIES = {
    "web": "The web interface process stopped unexpectedly; recording continues in its own process",
    "recorder": "The recorder process stopped unexpectedly; footage is not being written until it restarts",
    "exporter": "The export process stopped unexpectedly; running exports failed and queued exports wait for it to restart",
    "ai": "The AI analysis process stopped unexpectedly; recording continues but no analysis happens until it restarts",
    "rules": "The rules process stopped unexpectedly; new detections are evaluated when it restarts",
    "background": "The webhook, push, heartbeat and backup process stopped unexpectedly; notifications and backups wait until it restarts",
}
# AI worker faults use the ai_ prefix so they notify as ai.failure/ai.recovered.
FAULT_TYPES = {"ai": "ai_worker"}


@dataclass
class Worker:
    name: str
    module: str
    process: subprocess.Popen | None = None
    started_at: float = 0.0
    next_start_at: float = 0.0
    consecutive_failures: int = 0
    incident_active: bool = False
    pending_restart: bool = field(default=False)

    def start(self) -> None:
        environment = {**os.environ, "NVR_SUPERVISED": "1"}
        self.process = subprocess.Popen([sys.executable, "-m", self.module], env=environment)
        self.started_at = time.monotonic()
        liveness.record(self.name, state="starting", pid=self.process.pid, started=True, restarted=self.pending_restart)
        self.pending_restart = False
        logger.info("Started %s worker (pid %s)", self.name, self.process.pid)


class Supervisor:
    def __init__(self, workers: list[Worker]) -> None:
        self.workers = workers
        self.stopping = False

    def request_stop(self, *_: object) -> None:
        self.stopping = True

    def run(self) -> int:
        for worker in self.workers:
            worker.start()
        while not self.stopping:
            for worker in self.workers:
                self._watch(worker)
            time.sleep(POLL_SECONDS)
        self._stop_all()
        return 0

    def _watch(self, worker: Worker) -> None:
        now = time.monotonic()
        process = worker.process
        if process is None:
            if now >= worker.next_start_at:
                worker.start()
            return
        code = process.poll()
        if code is None and self._hung(worker, now):
            logger.warning("%s worker stopped reporting liveness; restarting it", worker.name)
            process.kill()
            code = process.wait()
        if code is not None:
            self._exited(worker, code, now)
            return
        if now - worker.started_at >= STABLE_AFTER_SECONDS:
            worker.consecutive_failures = 0
        if worker.incident_active and self._reporting(worker):
            self._incident(worker, active=False)

    def _exited(self, worker: Worker, code: int, now: float) -> None:
        worker.process = None
        worker.consecutive_failures += 1
        delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (worker.consecutive_failures - 1))
        worker.next_start_at = now + delay
        worker.pending_restart = True
        logger.error("%s worker exited with code %s; restarting in %.0fs", worker.name, code, delay)
        try:
            liveness.record(worker.name, state="crashed", exit_code=code, exited=True)
        except Exception:  # noqa: BLE001 - keep supervising even if the database is unavailable
            logger.exception("Could not record %s worker exit", worker.name)
        self._incident(worker, active=True, details={"exit_code": code, "restart_in_seconds": round(delay)})
        if worker.name == "ai":
            try:
                mark_analysis_interrupted("AI analysis process stopped; analysis resumes when it restarts")
            except Exception:  # noqa: BLE001
                logger.exception("Could not mark AI analysis interrupted")

    def _hung(self, worker: Worker, now: float) -> bool:
        if now - worker.started_at < liveness.STALE_AFTER.total_seconds():
            return False
        try:
            return not self._reporting(worker)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _reporting(worker: Worker) -> bool:
        with connect() as db:
            row = db.execute("SELECT * FROM worker_status WHERE name=?", (worker.name,)).fetchone()
        return bool(row) and row["pid"] == (worker.process.pid if worker.process else None) and liveness.is_fresh(row)

    def _incident(self, worker: Worker, *, active: bool, details: dict[str, object] | None = None) -> None:
        try:
            with connect() as db:
                sync_incident(
                    db,
                    incident_key=f"worker:{worker.name}",
                    camera_id=None,
                    fault_type=FAULT_TYPES.get(worker.name, f"worker_{worker.name}"),
                    summary=SUMMARIES[worker.name] if active else f"The {worker.name} process is running again",
                    active=active,
                    details={"worker": worker.name, "observed_at": datetime.now(timezone.utc).isoformat(), **(details or {})},
                )
            worker.incident_active = active
        except Exception:  # noqa: BLE001
            logger.exception("Could not record %s worker incident", worker.name)

    def _stop_all(self) -> None:
        # Stop accepting requests first; recording stops last.
        for worker in sorted(self.workers, key=lambda item: (item.name != "web", item.name == "recorder")):
            if worker.process and worker.process.poll() is None:
                liveness.record(worker.name, state="stopping")
                worker.process.send_signal(signal.SIGTERM)
                try:
                    worker.process.wait(timeout=STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    logger.warning("%s worker did not stop in time; killing it", worker.name)
                    worker.process.kill()
                    worker.process.wait()
            liveness.record(worker.name, state="stopped")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.umask(0o077)
    if backup.pre_migration_backup not in migrations.pre_migration_hooks:
        migrations.pre_migration_hooks.append(backup.pre_migration_backup)
    initialize()
    backup.reconcile_after_restore()
    supervisor = Supervisor([
        Worker("recorder", "app.workers.recorder"),
        Worker("exporter", "app.workers.exporter"),
        Worker("ai", "app.workers.ai"),
        Worker("rules", "app.workers.rules"),
        Worker("background", "app.workers.background"),
        Worker("web", "app.workers.web"),
    ])
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
