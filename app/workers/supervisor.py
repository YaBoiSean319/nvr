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

from app import backup, db_health, failover, logforward, migrations
from app.detection import mark_analysis_interrupted
from app.incidents import sync_incident
from app.store import connect, initialize
from app.workers import liveness, process_info

logger = logging.getLogger("nvr.supervisor")

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
STABLE_AFTER_SECONDS = 60.0
MEMORY_SAMPLE_SECONDS = 5.0
POLL_SECONDS = 0.5
# The whole stop, every worker included: under Compose's stop_grace_period (30 s), after which Docker kills everything without cleanup (#410).
STOP_GRACE_SECONDS = float(os.environ.get("NVR_STOP_GRACE_SECONDS", "25"))
# The workers other than the recorder stop together within this share of it; the recorder, stopped last, always has the rest to finish
# and index the segments it is writing.
OTHERS_SHARE = 0.5
STATE_WRITE_SECONDS = 2.0  # The most the stop waits, in all, to write the workers' states while the recorder may be writing its last segments

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


def end_group(process: subprocess.Popen) -> bool:
    """End every process still in the worker's process group (its FFmpeg children and anything else it started); True if there were any.

    A worker is started as the leader of its own group, so its children share the group id. A worker that is killed (by this supervisor for a
    stalled loop, or by the kernel for lack of memory) cannot run its own cleanup, and its FFmpeg processes would otherwise keep running,
    still connected to the cameras, while the restarted worker started a second set on the same folders (#322).
    """
    try:
        os.killpg(process.pid, 0)  # Is anyone left in the group?
    except (ProcessLookupError, PermissionError):
        return False
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return False
    return True


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
    killed_reason: str | None = None
    last_rss: int | None = None
    peak_rss: int | None = None
    rss_sampled_at: float = 0.0

    def start(self) -> None:
        environment = {**os.environ, "NVR_SUPERVISED": "1"}
        self.process = subprocess.Popen([sys.executable, "-m", self.module], env=environment, start_new_session=True)  # Its own process group: see end_group
        self.started_at = time.monotonic()
        self.killed_reason, self.last_rss, self.peak_rss, self.rss_sampled_at = None, None, None, 0.0
        try:
            liveness.pulse_path(self.name).unlink(missing_ok=True)  # The last process's pulse says nothing about this one
        except OSError:
            pass
        _record(self.name, state="starting", pid=self.process.pid, started=True, restarted=self.pending_restart)
        self.pending_restart = False
        logger.info("Started %s worker (pid %s)", self.name, self.process.pid)


def _record(name: str, **state: object) -> None:
    """Write a worker's state for the status page. The supervisor carries on if the database cannot be written: it used to exit, and being the
    container's first process took everything down with it (#336)."""
    try:
        liveness.record(name, **state)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - supervising matters more than the status row
        logger.exception("Could not record the %s worker's state (%s); carrying on", name, state.get("state"))


class Supervisor:
    def __init__(self, workers: list[Worker]) -> None:
        self.workers = workers
        self.stopping = False
        self._oom_kills_seen = process_info.oom_kills()

    def request_stop(self, *_: object) -> None:
        self.stopping = True

    def run(self) -> int:
        for worker in self.workers:
            worker.start()
        while not self.stopping:
            for worker in self.workers:
                try:
                    self._watch(worker)
                except Exception:  # noqa: BLE001 - one bad pass must not end supervision of every worker
                    logger.exception("Watching the %s worker failed; carrying on", worker.name)
            self._reap_orphans()
            time.sleep(POLL_SECONDS)
        self._stop_all()
        return 0

    def _reap_orphans(self) -> None:
        """Collect processes that are not workers and have ended. When a worker is killed, what it left running (FFmpeg) is ended too and, in a
        container, is then this process's to collect; uncollected, each stays in the process table as a zombie until the container restarts."""
        workers = {worker.process.pid for worker in self.workers if worker.process is not None}
        for _ in range(256):
            try:
                ended = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)  # Look without collecting
                if ended is None or ended.si_pid in workers:
                    return  # Nothing has ended, or a worker has: its own watch collects it
                os.waitpid(ended.si_pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                return

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
            worker.killed_reason = (f"killed by the supervisor: no heartbeat for {liveness.STALE_AFTER.total_seconds():.0f} s (the process was alive but its event loop was blocked or starved; "
                                    "the heartbeat the supervisor reads is a file, not a database write, so a busy database does not cause this)")
            end_group(process)  # Its children first: a SIGKILLed worker cannot stop them itself
            process.kill()
            code = process.wait()
        if code is not None:
            if end_group(process):
                logger.warning("Ended processes the %s worker left running (for example FFmpeg) so the restarted worker does not start a second set", worker.name)
            self._exited(worker, code, now)
            return
        self._sample_memory(worker, now)
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
        reason = self._explain(worker, code)
        logger.error("%s worker exited with code %s (%s); restarting in %.0fs", worker.name, code, reason, delay)
        try:
            liveness.record(worker.name, state="crashed", exit_code=code, exited=True)
        except Exception:  # noqa: BLE001 - keep supervising even if the database is unavailable
            logger.exception("Could not record %s worker exit", worker.name)
        if worker.name == "ai":
            # Before the incident is announced, so that whoever reacts to it finds analysis already marked interrupted.
            try:
                mark_analysis_interrupted("AI analysis process stopped; analysis resumes when it restarts")
            except Exception:  # noqa: BLE001
                logger.exception("Could not mark AI analysis interrupted")
        self._incident(worker, active=True, details={
            "exit_code": code, "reason": reason, "restart_in_seconds": round(delay),
            "memory_mb_at_last_sample": process_info.megabytes(worker.last_rss), "peak_memory_mb": process_info.megabytes(worker.peak_rss),
            "container_memory_limit_mb": process_info.megabytes(process_info.memory_limit_bytes()),
        })

    @staticmethod
    def _sample_memory(worker: Worker, now: float) -> None:
        """Remember the worker's memory every few seconds, so that a later exit can say how much it was using."""
        if worker.process is None or now - worker.rss_sampled_at < MEMORY_SAMPLE_SECONDS:
            return
        worker.rss_sampled_at = now
        rss = process_info.rss_bytes(worker.process.pid)
        if rss is not None:
            worker.last_rss = rss
            worker.peak_rss = max(rss, worker.peak_rss or 0)

    def _explain(self, worker: Worker, code: int) -> str:
        """Why a worker is gone: a SIGKILL (-9) comes either from this supervisor or from the kernel, and the two need different fixes."""
        kills = process_info.oom_kills()  # Read on every exit, so one kill is attributed once and never to a later, unrelated exit
        newly_killed = kills is not None and self._oom_kills_seen is not None and kills > self._oom_kills_seen
        if kills is not None:
            self._oom_kills_seen = kills
        if worker.killed_reason:
            return worker.killed_reason
        if code == -signal.SIGKILL:
            memory = f"; it was using about {process_info.megabytes(worker.last_rss)} MB when last measured" if worker.last_rss else ""
            limit = process_info.megabytes(process_info.memory_limit_bytes())
            limit_text = f" (container limit {limit} MB)" if limit else " (no container memory limit is set)"
            if newly_killed:
                return f"killed by the kernel: a process in the container was killed for lack of memory{memory}{limit_text}"
            return f"killed by SIGKILL from outside the supervisor; the usual cause is the host's out-of-memory killer{memory}{limit_text}"
        if code < 0:
            return f"killed by signal {-code}"
        return "exited by itself"

    def _hung(self, worker: Worker, now: float) -> bool:
        if now - worker.started_at < liveness.STALE_AFTER.total_seconds():
            return False
        return not self._reporting(worker)

    @staticmethod
    def _reporting(worker: Worker) -> bool:
        """Whether the worker's event loop is turning, read from its pulse file: never from the database, which may be the thing that is stuck."""
        return liveness.pulse_fresh(worker.name, worker.process.pid if worker.process else None)

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
        """Stop every worker within STOP_GRACE_SECONDS in all. Every worker but the recorder is asked at once (the web process first, so it
        stops accepting requests) and given OTHERS_SHARE of the time; the recorder is stopped last with what is left. A worker that has not
        stopped by its deadline is killed with everything it started. Workers used to be stopped one after another, each given the whole
        grace period, so a slow one pushed the recorder past Docker's limit and it was killed without finishing its segments (#410)."""
        started = time.monotonic()
        deadline = started + STOP_GRACE_SECONDS
        others = sorted((worker for worker in self.workers if worker.name != "recorder"), key=lambda item: item.name != "web")
        recorders = [worker for worker in self.workers if worker.name == "recorder"]
        self._record_states(self.workers, "stopping", started)
        self._stop_together(others, started + STOP_GRACE_SECONDS * OTHERS_SHARE, started)
        self._stop_together(recorders, deadline, started)
        self._record_states(self.workers, "stopped", time.monotonic())
        logger.info("Every worker stopped %.1f s after the stop was asked for", time.monotonic() - started)

    def _stop_together(self, workers: list[Worker], deadline: float, started: float) -> None:
        running = [worker for worker in workers if worker.process and worker.process.poll() is None]
        for worker in running:
            worker.process.send_signal(signal.SIGTERM)
        waiting = list(running)
        while waiting:
            for worker in list(waiting):
                code = worker.process.poll()
                if code is not None:
                    waiting.remove(worker)
                    logger.info("%s worker stopped after %.1f s (exit code %s)", worker.name, time.monotonic() - started, code)
            if not waiting or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        for worker in waiting:
            logger.warning("%s worker did not stop within %.1f s; killing it", worker.name, time.monotonic() - started)
            end_group(worker.process)
            worker.process.kill()
            worker.process.wait()
        for worker in running:
            if end_group(worker.process):
                logger.warning("Ended processes the %s worker left running when it stopped", worker.name)

    def _record_states(self, workers: list[Worker], state: str, since: float) -> None:
        """Write each worker's state for the status page, waiting at most STATE_WRITE_SECONDS in all for the database: the stop must not be held
        up by it. What cannot be written is skipped and logged once."""
        for worker in workers:
            left = STATE_WRITE_SECONDS - (time.monotonic() - since)
            if left <= 0.05:
                logger.warning("No time left to record the workers' %s state; the status page shows it when they start again", state)
                return
            try:
                liveness.record(worker.name, state=state, busy_seconds=left)
            except Exception:  # noqa: BLE001 - stopping matters more than the status row
                logger.exception("Could not record the workers' %s state; carrying on", state)
                return

def main() -> int:
    started = time.monotonic()
    # The workers say how long after this the web interface first answered: the health check allows that long before it counts failures (#418).
    os.environ["NVR_STARTED_AT"] = repr(time.time())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logforward.install("supervisor")
    os.umask(0o077)
    if backup.pre_migration_backup not in migrations.pre_migration_hooks:
        migrations.pre_migration_hooks.append(backup.pre_migration_backup)
    db_health.log_at_startup()  # Before anything else touches the file, so a damaged one is named first and not copied by the pre-migration backup unnoticed
    initialize()
    backup.reconcile_after_restore()
    failover.prepare_startup()  # A primary does not record until it has heard from the standby
    logger.info("The database was ready %.1f s after the NVR started; starting the workers", time.monotonic() - started)
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
