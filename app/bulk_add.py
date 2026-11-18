"""Add many cameras from a list of addresses and logins, as a job the server runs (#173).

The browser used to check and add the list one camera at a time, so closing the page ended the run and lost its progress, and a long list
held the page open for as long as it took. A job is kept in the database instead: ``create_job`` stores the list, the runner (in the web
process, three cameras at a time, on top of the limit on inspections) checks and adds each camera exactly as the Add Camera form does, and the
page only shows the job's progress, which it can pick up again after being closed.

A camera's password is kept encrypted, and only until its item is finished: it is cleared as soon as the item ends, whatever the outcome, and
when the job is cancelled. Nothing in a job's view holds a password.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from . import camera_inspect
from .audit import record
from .clock import stamp
from .credentials import decrypt, encrypt
from .onvif_client import normalise_host
from .store import connect, has_row

logger = logging.getLogger(__name__)

MAX_CAMERAS = 500
CONCURRENCY = 3  # Cameras checked at once, however long the list; inspections are limited again by camera_inspect
KEEP_DAYS = 7
RECENT_HOURS = 1
POLL_SECONDS = 2.0
_ADDRESS = re.compile(r"^(?:\[([0-9A-Fa-f:.]+)\]|([^:\s\[\]]+)):(\d{1,5})$")


def split_address(text: str) -> tuple[str, str | None]:
    """An address as typed: a host name or IP address, optionally with ``:port`` for a camera whose ONVIF service is not on a usual port (IPv6 in brackets)."""
    trimmed = text.strip()
    match = _ADDRESS.match(trimmed)
    port = int(match.group(3)) if match else 0
    if not match or not 1 <= port <= 65535:
        return (trimmed[1:-1] if trimmed.startswith("[") and trimmed.endswith("]") else trimmed), None
    host = match.group(1) or match.group(2)
    return host, f"http://{f'[{host}]' if ':' in host else host}:{port}/onvif/device_service"


def suggested_name(report: dict[str, Any] | None, host: str) -> str:
    device = (report or {}).get("device") or {}
    named = " ".join(part for part in (device.get("manufacturer"), device.get("model")) if part).strip()
    typed = host.strip()  # With its port, so two cameras on one host are named apart
    return f"{named} {typed}" if named else f"Camera {typed}"


def attention_text(report: dict[str, Any]) -> str:
    """What is wrong, in a sentence, for a camera that was checked but is not fully working."""
    failed = next((step for step in report.get("steps", []) if step.get("state") == "failed" and step.get("id") != "ready"), None)
    return f"{failed.get('label')}: {failed.get('detail') or 'failed'}" if failed else "Video could not be verified"


def _detail_of(exc: BaseException) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, list):  # Validation errors: what is wrong and where, never what was sent
            return "; ".join(str(item.get("msg", "Invalid value")) if isinstance(item, dict) else str(item) for item in detail)[:300]
        return str(detail)[:300]
    if isinstance(exc, ValidationError):
        return "; ".join(str(error.get("msg", "Invalid value")).removeprefix("Value error, ") for error in exc.errors())[:300]
    return str(exc)[:300] or "The camera could not be added"


# --- the job's rows -------------------------------------------------------------------------------------------------------------------


def create_job(cameras: list[dict[str, Any]], *, created_by: int | None, add_anyway: bool, recording: bool) -> str:
    """Store a list of cameras to add. Each is ``{"host", "username", "password", "name"}``; passwords are encrypted. Returns the job's id."""
    job_id = uuid.uuid4().hex
    now = stamp()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        prune(db)
        db.execute("INSERT INTO camera_add_jobs(id,created_at,created_by,state,add_anyway,recording) VALUES(?,?,?,'running',?,?)", (job_id, now, created_by, int(add_anyway), int(recording)))
        for number, camera in enumerate(cameras, start=1):
            password = camera.get("password") or ""
            db.execute(
                "INSERT INTO camera_add_items(job_id,row_number,host,name,username,password_ciphertext,status,updated_at) VALUES(?,?,?,?,?,?,'queued',?)",
                (job_id, number, camera["host"].strip(), (camera.get("name") or "").strip(), camera.get("username") or None, encrypt(password) if password else None, now),
            )
        record(db, created_by, "camera_bulk_add_started", "camera_add_job", job_id, cameras=len(cameras), add_anyway=add_anyway, recording=recording)
        db.execute("COMMIT")
    return job_id


def prune(db) -> None:
    """Forget finished jobs after a week, and make sure no finished item still holds a password."""
    cutoff = stamp(datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS))
    db.execute("DELETE FROM camera_add_jobs WHERE state!='running' AND finished_at IS NOT NULL AND finished_at<?", (cutoff,))
    db.execute("UPDATE camera_add_items SET password_ciphertext=NULL WHERE password_ciphertext IS NOT NULL AND status NOT IN ('queued','checking','adding')")


def job_view(db, job_id: str) -> dict[str, Any] | None:
    job = db.execute("SELECT * FROM camera_add_jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        return None
    items = db.execute("SELECT row_number,host,name,status,detail,camera_id FROM camera_add_items WHERE job_id=? ORDER BY row_number", (job_id,)).fetchall()
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "id": job["id"], "state": job["state"], "created_at": job["created_at"], "finished_at": job["finished_at"],
        "add_anyway": bool(job["add_anyway"]), "recording": bool(job["recording"]), "counts": counts,
        "items": [{"row": item["row_number"], "host": item["host"], "name": item["name"], "status": item["status"], "detail": item["detail"], "camera_id": item["camera_id"]} for item in items],
    }


def latest_job_id(db) -> str | None:
    """The job still running, or the newest one that finished within the last hour, so a page opened again shows what happened."""
    cutoff = stamp(datetime.now(timezone.utc) - timedelta(hours=RECENT_HOURS))
    row = db.execute("SELECT id FROM camera_add_jobs WHERE state='running' OR finished_at>=? ORDER BY created_at DESC LIMIT 1", (cutoff,)).fetchone()
    return row["id"] if row else None


def cancel_job(job_id: str) -> bool:
    """Stop a job: cameras not yet started are not added (and their passwords are cleared); one being checked finishes. False if there is no such job."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute("SELECT state FROM camera_add_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            db.execute("ROLLBACK")
            return False
        if job["state"] == "running":
            db.execute("UPDATE camera_add_items SET status='skipped',detail='Stopped before this camera',password_ciphertext=NULL,updated_at=? WHERE job_id=? AND status='queued'", (stamp(), job_id))
            db.execute("UPDATE camera_add_jobs SET state='cancelled',finished_at=? WHERE id=?", (stamp(), job_id))
        db.execute("COMMIT")
    return True


def _close_if_finished(db, job_id: str) -> None:
    if not has_row(db, "SELECT 1 FROM camera_add_items WHERE job_id=? AND status IN ('queued','checking','adding')", (job_id,)):
        db.execute("UPDATE camera_add_jobs SET state='done',finished_at=? WHERE id=? AND state='running'", (stamp(), job_id))


def _set(item_id: int, status: str, detail: str = "", camera_id: str | None = None, *, finished: bool = False) -> None:
    with connect() as db:
        if finished:
            db.execute("UPDATE camera_add_items SET status=?,detail=?,camera_id=?,password_ciphertext=NULL,updated_at=? WHERE id=?", (status, detail, camera_id, stamp(), item_id))
            job_id = db.execute("SELECT job_id FROM camera_add_items WHERE id=?", (item_id,)).fetchone()["job_id"]
            _close_if_finished(db, job_id)
        else:
            db.execute("UPDATE camera_add_items SET status=?,detail=?,updated_at=? WHERE id=?", (status, detail, stamp(), item_id))


def claim(limit: int) -> list[dict[str, Any]]:
    """Take up to ``limit`` waiting cameras (oldest job first, in list order) and mark them as being checked. Reads before it locks, so idle costs nothing (#280)."""
    with connect() as db:
        if not has_row(db, "SELECT 1 FROM camera_add_items i JOIN camera_add_jobs j ON j.id=i.job_id WHERE i.status='queued' AND j.state='running'"):
            return []
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT i.*,j.add_anyway,j.recording FROM camera_add_items i JOIN camera_add_jobs j ON j.id=i.job_id "
            "WHERE i.status='queued' AND j.state='running' ORDER BY j.created_at, i.row_number LIMIT ?", (limit,)).fetchall()
        for row in rows:
            db.execute("UPDATE camera_add_items SET status='checking',updated_at=? WHERE id=?", (stamp(), row["id"]))
        db.execute("COMMIT")
        return [dict(row) for row in rows]


def recover_interrupted() -> None:
    """After a restart: a camera that was only being checked goes back in the queue; one that was being saved may or may not have been, so it is reported, not repeated."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE camera_add_items SET status='queued',updated_at=? WHERE status='checking'", (stamp(),))
        stopped = db.execute("SELECT DISTINCT job_id FROM camera_add_items WHERE status='adding'").fetchall()
        db.execute("UPDATE camera_add_items SET status='failed',detail='Interrupted while saving: check the camera list before adding it again',password_ciphertext=NULL,updated_at=? WHERE status='adding'", (stamp(),))
        for row in stopped:
            _close_if_finished(db, row["job_id"])
        db.execute("COMMIT")


# --- the work -------------------------------------------------------------------------------------------------------------------------


async def process(item: dict[str, Any], recorders: Any) -> None:
    """Check one camera and add it, as the Add Camera form does, and record the outcome. Never raises."""
    from app.api.cameras import CameraCreateRequest, add_camera

    item_id = item["id"]
    try:
        password = decrypt(item["password_ciphertext"]) if item["password_ciphertext"] else ""
        username = item["username"] or ""
        if bool(username) != bool(password):
            raise ValueError("Provide both camera username and password, or leave both blank")
        host, endpoint = split_address(item["host"])
        host = normalise_host(host)
        report = await camera_inspect.inspect_camera(host, username, password, device_service_url=endpoint, brand="auto", verify=True)
        if not report["ok"] and not item["add_anyway"]:
            _set(item_id, "failed", f"Not added because it needs attention: {attention_text(report)}", finished=True)
            return
        _set(item_id, "adding")
        selection = report.get("selection") or {}
        payload = CameraCreateRequest(
            name=item["name"] or suggested_name(report, item["host"]), camera_ip=host, camera_username=username or None, camera_password=password or None,
            camera_brand=report["brand"] if report.get("brand") and report["brand"] != "auto" else "auto",
            device_service_url=report.get("device_service_url") or endpoint,
            onvif_profile_token=selection.get("record_token"),
            live_substream_profile_token=selection["live_token"] if selection.get("live_token") and selection.get("live_verified") is not False else None,
            recording_mode="motion", recording_enabled=bool(item["recording"]) and bool(report["ok"]), save_anyway=not report["ok"],
        )
        camera = await add_camera(payload, recorders)
        if report["ok"]:
            _set(item_id, "added", camera["name"], camera["id"], finished=True)
        else:
            _set(item_id, "attention", f"{camera['name']}: {attention_text(report)}", camera["id"], finished=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - one camera's failure is that camera's result, never the run's
        if not isinstance(exc, (ValueError, HTTPException, ValidationError)):
            logger.exception("Adding a camera in a bulk job failed")
        _set(item_id, "failed", _detail_of(exc), finished=True)


class BulkAddRunner:
    """Works through queued cameras, ``CONCURRENCY`` at a time, in the web process (which holds the recorder client a new camera must be announced to)."""

    def __init__(self, recorders: Any = None) -> None:
        self._recorders = recorders  # A value, or a function returning it (the app's recorders are set up after this object)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._running: set[asyncio.Task[None]] = set()

    def wake(self) -> None:
        self._wake.set()

    def _recorder_client(self) -> Any:
        return self._recorders() if callable(self._recorders) else self._recorders

    async def start(self) -> None:
        await asyncio.to_thread(recover_interrupted)
        self._task = asyncio.create_task(self._run(), name="bulk-add")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        tasks = [task for task in (self._task, *self._running) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task, self._running = None, set()

    async def run_until_idle(self) -> None:
        """Work through everything queued, then return (the loop's own pass, for tests and for a caller that wants to wait)."""
        while True:
            self._running = {task for task in self._running if not task.done()}
            free = CONCURRENCY - len(self._running)
            claimed = await asyncio.to_thread(claim, free) if free > 0 else []
            for item in claimed:
                self._running.add(asyncio.create_task(process(item, self._recorder_client())))
            if not self._running:
                return
            await asyncio.wait(self._running, return_when=asyncio.FIRST_COMPLETED)

    async def _run(self) -> None:
        while True:
            try:
                await self.run_until_idle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - tried again on the next pass
                logger.exception("The bulk camera add loop failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
