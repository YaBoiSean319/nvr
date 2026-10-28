"""Edge backfill (#131): after an outage, fetch what the camera recorded on its own storage and add it to the timeline.

For a camera with backfill switched on, the NVR watches for outages (the gaps the coverage record explains by the camera, its stream or the
NVR's own restart; never ones its schedule expects), and for each asks the camera, over ONVIF Profile G, what it recorded then: the Search
service lists its recordings and the Replay service gives the address to play one from. The span is fetched in short chunks with FFmpeg
without re-encoding (``-c copy``), and each chunk becomes a segment marked ``backfill`` in the index, so it plays like any other footage and
the timeline can show where it came from.

* **Opt-in, per camera**, with a bandwidth limit (kbit/s). One chunk is fetched at a time, anywhere, and FFmpeg reads at the rate that keeps
  the stream at or under the limit, worked out from the bitrate seen on the first chunk.
* **Resumable.** A job keeps the point it has filled up to, so after an interruption or a restart it carries on from there. A transient
  failure waits (a minute, doubling, up to half an hour) and gives up after a few tries.
* **Not supported is not retried.** A camera that offers no Search or Replay service is recorded as unsupported, once, and left alone until
  an administrator turns backfill off and on again or asks for a new check.
* The timeline's gaps say what is happening to them (``gap["backfill"]``) and filled footage is marked backfilled.

The camera's replay address is taken on the host the administrator entered, as every ONVIF address is, and never logged: it carries the login.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import coverage
from .credentials import decrypt
from .onvif_client import OnvifClient, OnvifError, Recording, Services
from .recording import recordings_path
from .storage import camera_recordings_dir
from .store import connect

log = logging.getLogger(__name__)

POLL_SECONDS = 30.0
LOOKBACK = timedelta(hours=24)  # How far back an outage is worth asking the camera about
SETTLE = timedelta(seconds=90)  # An outage this close to now may still be going on
MIN_GAP = timedelta(seconds=30)
CHUNK = timedelta(seconds=60)
FIRST_CHUNK = timedelta(seconds=10)  # Short, so the bitrate is known before a long one is fetched
DEFAULT_LIMIT_KBPS = 8192
MIN_LIMIT_KBPS, MAX_LIMIT_KBPS = 64, 1_000_000
MAX_ATTEMPTS = 6
BACKOFF_FIRST, BACKOFF_MAX = timedelta(minutes=1), timedelta(minutes=30)
FETCH_SLACK_SECONDS = 90
PROTOCOLS = "concat,pipe,file,http,https,rtsp,rtsps,tcp,udp,rtp,tls,crypto"
# Gaps the camera's own recording can fill: it was running; the NVR was not getting its video (or not running).
OUTAGES = frozenset({"camera_offline", "stream_error", "worker_restart", "unknown"})
STATE_WORDS = {"pending": "waiting", "running": "running", "done": "filled", "partial": "partly filled", "failed": "failed", "unsupported": "unsupported"}
NOT_SUPPORTED = "Backfill not supported: this camera offers no ONVIF Profile G recording search and replay"
NO_ADDRESS = "Backfill not supported: the camera has no ONVIF address"
SUPPORTED = "The camera offers ONVIF recording search and replay"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def limit_kbps(row: sqlite3.Row) -> int:
    return int(row["backfill_limit_kbps"] or DEFAULT_LIMIT_KBPS)


def readrate_for(limit: int, bitrate_kbps: float | None) -> float:
    """FFmpeg's ``-readrate`` (a multiple of real time) that keeps a stream of ``bitrate_kbps`` at or under ``limit`` kbit/s; real time until the bitrate is known."""
    if not bitrate_kbps or bitrate_kbps <= 0:
        return 1.0
    return max(0.02, min(50.0, limit / bitrate_kbps))


def backoff(attempts: int) -> timedelta:
    return min(BACKOFF_MAX, BACKOFF_FIRST * 2 ** max(0, attempts - 1))


# ----------------------------------------------------------------------------------------------------------------------- support

def _credentials(row: sqlite3.Row) -> tuple[str, str]:
    raw = json.loads(decrypt(row["motion_credentials_ciphertext"])) if row["motion_credentials_ciphertext"] else {}
    return raw.get("username", ""), raw.get("password", "")


def _remember_support(camera_id: str, state: str | None, message: str) -> None:
    with connect() as db:
        db.execute("UPDATE cameras SET backfill_support=?,backfill_support_message=?,backfill_checked_at=? WHERE id=?", (state, message[:300], _now().isoformat(), camera_id))


async def _connect(row: sqlite3.Row) -> tuple[OnvifClient, Services]:
    """The camera's ONVIF client and services; the caller closes the client. ``OnvifError`` says why it could not be reached."""
    url = row["device_service_url"]
    parsed = urlsplit(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise OnvifError("unsupported", "The camera has no ONVIF address")
    username, password = _credentials(row)
    client = OnvifClient(parsed.hostname, username, password)
    try:
        device = await client.locate_device(url)
        return client, await client.services(device)
    except BaseException:
        await client.close()
        raise


async def check_support(camera_id: str) -> dict[str, Any]:
    """Ask the camera whether it offers Profile G search and replay, and remember the answer. A camera that cannot be reached is not 'unsupported'."""
    with connect() as db:
        row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if row is None:
        raise LookupError("Camera not found")
    try:
        client, services = await _connect(row)
    except OnvifError as exc:
        if exc.code == "unsupported":
            mark_unsupported(camera_id, NO_ADDRESS)
        else:
            _remember_support(camera_id, None, f"Not checked yet: {exc.message}")
        return support_view(camera_id)
    async with client:
        have = {"search", "replay"} <= set(services.urls)
    if have:
        _remember_support(camera_id, "supported", SUPPORTED)
    else:
        mark_unsupported(camera_id, NOT_SUPPORTED)
    return support_view(camera_id)


def mark_unsupported(camera_id: str, message: str) -> None:
    """Recorded once, for the camera and its waiting jobs; nothing asks the camera again until an administrator asks for a new check."""
    _remember_support(camera_id, "unsupported", message)
    with connect() as db:
        db.execute("UPDATE backfill_jobs SET state='unsupported',message=?,next_attempt_at=NULL,updated_at=? WHERE camera_id=? AND state IN ('pending','running')",
                   (message, _now().isoformat(), camera_id))


def support_view(camera_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT backfill_support,backfill_support_message,backfill_checked_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
    return {"state": row["backfill_support"] or "unknown", "message": row["backfill_support_message"] or "", "checked_at": row["backfill_checked_at"]}


# ------------------------------------------------------------------------------------------------------------------------ planning

def plan_jobs(db: sqlite3.Connection, camera_id: str, now: datetime) -> int:
    """Start a job for each outage in the lookback that has none yet. Returns how many were started."""
    end = now - SETTLE
    camera = db.execute("SELECT created_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
    begin = max(now - LOOKBACK, _parse(camera["created_at"])) if camera else now - LOOKBACK  # Time before the camera was added is not an outage
    if begin >= end:
        return 0
    found = coverage.timeline(db, camera_id, begin, end, limit=None)["gaps"]
    merged: list[list[datetime]] = []
    for gap in found:
        if gap["reason_code"] not in OUTAGES:
            continue
        start, stop = _parse(gap["starts_at"]), _parse(gap["ends_at"])
        if merged and merged[-1][1] == start:
            merged[-1][1] = stop
        else:
            merged.append([start, stop])
    started = 0
    for start, stop in merged:
        if stop >= end or stop - start < MIN_GAP:
            continue  # Still going on, or too short to be worth a request
        if db.execute("SELECT 1 FROM backfill_jobs WHERE camera_id=? AND starts_at<? AND ends_at>? LIMIT 1", (camera_id, stop.isoformat(), start.isoformat())).fetchone():
            continue
        stamp = now.isoformat()
        db.execute("INSERT INTO backfill_jobs(id,camera_id,starts_at,ends_at,state,cursor_at,created_at,updated_at) VALUES(?,?,?,?,'pending',?,?,?)",
                   (str(uuid.uuid4()), camera_id, start.isoformat(), stop.isoformat(), start.isoformat(), stamp, stamp))
        started += 1
    return started


def annotate_gaps(db: sqlite3.Connection, camera_id: str, gaps: list[dict[str, Any]]) -> None:
    """Add ``backfill`` to each gap of a timeline: what is being done about it, or why nothing is."""
    if not gaps:
        return
    camera = db.execute("SELECT backfill_enabled,backfill_support,backfill_support_message FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if camera is None:
        return
    jobs = db.execute("SELECT state,starts_at,ends_at,message,cursor_at FROM backfill_jobs WHERE camera_id=? AND ends_at>? AND starts_at<?",
                      (camera_id, min(gap["starts_at"] for gap in gaps), max(gap["ends_at"] for gap in gaps))).fetchall()
    for gap in gaps:
        if gap["reason_code"] not in OUTAGES:
            gap["backfill"] = None
            continue
        job = next((item for item in jobs if item["starts_at"] < gap["ends_at"] and item["ends_at"] > gap["starts_at"]), None)
        if job is not None and job["state"] == "done":
            # The job filled this span, yet it is a gap again: retention has since deleted the backfilled footage.
            gap["backfill"] = {"state": "removed", "message": "The footage backfilled for this time is no longer stored"}
        elif job is not None:
            gap["backfill"] = {"state": STATE_WORDS[job["state"]], "message": job["message"]}
        elif not camera["backfill_enabled"]:
            gap["backfill"] = {"state": "off", "message": "Backfill is not turned on for this camera"}
        elif camera["backfill_support"] == "unsupported":
            gap["backfill"] = {"state": "unsupported", "message": camera["backfill_support_message"] or NOT_SUPPORTED}
        else:
            gap["backfill"] = {"state": "waiting", "message": "Backfill will try this gap shortly"}


def seconds_backfilled(db: sqlite3.Connection, camera_id: str, start: datetime, end: datetime) -> float:
    total = 0.0
    for row in db.execute("SELECT starts_at,ends_at FROM recording_segments WHERE camera_id=? AND source='backfill' AND starts_at<? AND ends_at>? AND unavailable_at IS NULL",
                          (camera_id, end.isoformat(), start.isoformat())):
        total += (min(end, _parse(row["ends_at"])) - max(start, _parse(row["starts_at"]))).total_seconds()
    return round(total, 1)


# ------------------------------------------------------------------------------------------------------------------------ fetching

async def _probe_seconds(path: Path) -> float:
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is unavailable")
    process = await asyncio.create_subprocess_exec(executable, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    try:
        return max(0.0, float(out.decode().strip()))
    except ValueError:
        return 0.0


async def fetch_chunk(url: str, offset: float, duration: float, target: Path, readrate: float) -> None:
    """Copy ``duration`` seconds of a replay, ``offset`` seconds into it, to ``target`` without re-encoding, no faster than ``readrate`` times real time.
    The address, which carries the login, goes to FFmpeg over stdin as the live recorder's does, so it is never in a process listing."""
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("FFmpeg is unavailable")
    manifest = (f"ffconcat version 1.0\nfile {url}\n" + ("option rtsp_transport tcp\n" if url.startswith("rtsp") else "") +
                f"inpoint {offset:.3f}\noutpoint {offset + duration:.3f}\n").encode()
    args = [executable, "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-protocol_whitelist", PROTOCOLS, "-readrate", f"{readrate:.3f}",
            "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-t", f"{duration:.3f}", "-movflags", "+faststart", "-y", str(target)]
    process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.communicate(manifest), timeout=duration / readrate + FETCH_SLACK_SECONDS)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
            await process.wait()
        target.unlink(missing_ok=True)
        raise
    if process.returncode != 0 or not target.is_file() or target.stat().st_size < 128:
        target.unlink(missing_ok=True)
        raise RuntimeError("The camera's replay could not be copied")


class BackfillManager:
    """Plans and runs backfill jobs, one chunk at a time, on the recorder's event loop."""

    def __init__(self, poll_seconds: float = POLL_SECONDS) -> None:
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        self.recover()
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="nvr-backfill")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.recover()

    @staticmethod
    def recover() -> None:
        """A job that was running when the application stopped carries on from the point it had filled up to."""
        with connect() as db:
            db.execute("UPDATE backfill_jobs SET state='pending',updated_at=? WHERE state='running'", (_now().isoformat(),))

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never ends the loop
                log.exception("Backfill could not run")
            await asyncio.sleep(self.poll_seconds)

    async def tick(self, now: datetime | None = None, max_chunks: int | None = None) -> int:
        """Plan outages, then work through the jobs that are due. Returns the chunks fetched. ``max_chunks`` stops early (a test of resuming)."""
        now = now or _now()
        with connect() as db:
            cameras = db.execute("SELECT id FROM cameras WHERE backfill_enabled=1 AND enabled=1 AND archived_at IS NULL AND (backfill_support IS NULL OR backfill_support='supported')").fetchall()
            for camera in cameras:
                plan_jobs(db, camera["id"], now)
        fetched = 0
        while not self._stopping:
            with connect() as db:
                job = db.execute(
                    "SELECT j.* FROM backfill_jobs j JOIN cameras c ON c.id=j.camera_id WHERE j.state='pending' AND (j.next_attempt_at IS NULL OR j.next_attempt_at<=?) "
                    "AND c.backfill_enabled=1 AND c.enabled=1 AND (c.backfill_support IS NULL OR c.backfill_support='supported') ORDER BY j.created_at LIMIT 1", (now.isoformat(),)).fetchone()
            if job is None:
                break
            done = await self._run(job, now, None if max_chunks is None else max_chunks - fetched)
            fetched += done
            if max_chunks is not None and fetched >= max_chunks:
                break
        return fetched

    @staticmethod
    def _update(job_id: str, **values: Any) -> None:
        values["updated_at"] = _now().isoformat()
        with connect() as db:
            db.execute(f"UPDATE backfill_jobs SET {','.join(f'{name}=?' for name in values)} WHERE id=?", (*values.values(), job_id))

    def _fail(self, job: sqlite3.Row, now: datetime, message: str) -> None:
        attempts = int(job["attempts"]) + 1
        if attempts >= MAX_ATTEMPTS:
            self._update(job["id"], state="failed", attempts=attempts, message=f"Gave up after {attempts} tries: {message}"[:300], next_attempt_at=None)
        else:
            self._update(job["id"], state="pending", attempts=attempts, message=f"Will try again: {message}"[:300], next_attempt_at=(now + backoff(attempts)).isoformat())

    async def _run(self, job: sqlite3.Row, now: datetime, budget: int | None) -> int:
        job_id, camera_id = job["id"], job["camera_id"]
        with connect() as db:
            row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
        self._update(job_id, state="running")
        try:
            client, services = await _connect(row)
        except OnvifError as exc:
            if exc.code == "unsupported":
                mark_unsupported(camera_id, NO_ADDRESS)
            else:
                self._fail(job, now, exc.message)
            return 0
        fetched = 0
        try:
            async with client:
                if not {"search", "replay"} <= set(services.urls):
                    mark_unsupported(camera_id, NOT_SUPPORTED)
                    return 0
                if row["backfill_support"] != "supported":
                    _remember_support(camera_id, "supported", SUPPORTED)
                recordings = await client.recordings(services)
                fetched = await self._fill(job, row, client, services, recordings, now, budget)
        except OnvifError as exc:
            self._fail(self._fresh(job_id), now, exc.message)
        except asyncio.CancelledError:
            self._update(job_id, state="pending")
            raise
        except Exception as exc:  # noqa: BLE001 - whatever went wrong, the job must not stay "running": it waits and tries again, then gives up
            log.exception("Backfill job %s failed", job_id)
            self._fail(self._fresh(job_id), now, str(exc)[:200] or "The camera's replay failed")
        return fetched

    @staticmethod
    def _fresh(job_id: str) -> sqlite3.Row:
        with connect() as db:
            return db.execute("SELECT * FROM backfill_jobs WHERE id=?", (job_id,)).fetchone()

    async def _fill(self, job: sqlite3.Row, row: sqlite3.Row, client: OnvifClient, services: Services, recordings: list[Recording], now: datetime, budget: int | None) -> int:
        job_id, camera_id = job["id"], job["camera_id"]
        end = _parse(job["ends_at"])
        cursor = _parse(job["cursor_at"])
        limit = limit_kbps(row)
        bitrate = None
        fetched = skipped = 0
        gone = False
        while cursor < end and not self._stopping and (budget is None or fetched < budget):
            with connect() as db:
                if not db.execute("SELECT 1 FROM cameras WHERE id=? AND backfill_enabled=1", (camera_id,)).fetchone():
                    self._update(job_id, state="pending", message="Backfill was turned off")
                    return fetched
            with connect() as db:
                # Live footage may have been indexed here since the gap was found: never fetch over it.
                live_until = db.execute("SELECT MAX(ends_at) FROM recording_segments WHERE camera_id=? AND starts_at<=? AND ends_at>? AND unavailable_at IS NULL",
                                        (camera_id, cursor.isoformat(), cursor.isoformat())).fetchone()[0]
                next_live = db.execute("SELECT MIN(starts_at) FROM recording_segments WHERE camera_id=? AND starts_at>? AND unavailable_at IS NULL", (camera_id, cursor.isoformat())).fetchone()[0]
            if live_until:
                cursor = max(cursor, _parse(live_until))
                continue
            recording = next((item for item in sorted(recordings, key=lambda item: item.earliest) if item.latest > cursor and item.earliest < end), None)
            if recording is None:
                gone = True
                break
            if recording.earliest > cursor:
                skipped += 1
                cursor = recording.earliest  # The camera has nothing before this
            length = FIRST_CHUNK if bitrate is None else CHUNK
            chunk_end = min(cursor + length, end, recording.latest, _parse(next_live) if next_live else end)
            seconds = (chunk_end - cursor).total_seconds()
            if seconds <= 0.5:
                cursor = chunk_end
                continue
            uri = client.replay_url_with_login(await client.replay_uri(services, recording.token))
            target = camera_recordings_dir(camera_id) / f"backfill-{cursor:%Y%m%dT%H%M%SZ}.mp4"
            target.parent.mkdir(parents=True, exist_ok=True)
            started = asyncio.get_running_loop().time()
            await fetch_chunk(uri, (cursor - recording.earliest).total_seconds(), seconds, target, readrate_for(limit, bitrate))
            elapsed = max(0.001, asyncio.get_running_loop().time() - started)
            actual = min(seconds, await _probe_seconds(target))
            if actual < 0.5:
                target.unlink(missing_ok=True)
                raise RuntimeError("The camera's replay held no footage for that time")
            advance = seconds if actual >= seconds - 1.0 else actual  # A chunk that came out short leaves no hole: the next one starts where it ended
            size = target.stat().st_size
            bitrate = size * 8 / 1000 / actual
            self._store(camera_id, target, cursor, cursor + timedelta(seconds=advance), size)
            cursor += timedelta(seconds=advance)
            fetched += 1
            progress = self._fresh(job_id)
            self._update(job_id, cursor_at=cursor.isoformat(), filled_seconds=float(progress["filled_seconds"]) + advance, bytes_fetched=int(progress["bytes_fetched"]) + size,
                         observed_kbps=int(size * 8 / 1000 / elapsed), state="running", attempts=0, next_attempt_at=None)
        if self._stopping or (budget is not None and fetched >= budget and cursor < end):
            self._update(job_id, state="pending")
            return fetched
        filled = float(self._fresh(job_id)["filled_seconds"])
        if cursor >= end and not skipped and not gone:
            self._update(job_id, state="done", cursor_at=end.isoformat(), message="Filled from the camera's own recording")
        else:
            self._update(job_id, state="partial" if filled else "failed", cursor_at=cursor.isoformat(),
                         message="The camera's own recording does not cover all of this gap (for example its card was removed or overwritten)" if filled
                         else "The camera has no recording of this time (for example its card was removed or overwritten)")
        return fetched

    @staticmethod
    def _store(camera_id: str, path: Path, starts: datetime, ends: datetime, size: int) -> None:
        relative = path.resolve().relative_to(recordings_path().resolve()).as_posix()
        with connect() as db:
            # A chunk fetched again after an interruption between its file and its cursor replaces the earlier row's figures.
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes,source) VALUES(?,?,?,?,?,'backfill') "
                       "ON CONFLICT(relative_path) DO UPDATE SET ends_at=excluded.ends_at,size_bytes=excluded.size_bytes",
                       (camera_id, relative, starts.isoformat(), ends.isoformat(), size))


def status(camera_id: str) -> dict[str, Any]:
    """What an administrator sees of a camera's backfill: the setting, what is known about support, and the recent jobs."""
    with connect() as db:
        row = db.execute("SELECT backfill_enabled,backfill_limit_kbps FROM cameras WHERE id=?", (camera_id,)).fetchone()
        jobs = [dict(job) for job in db.execute(
            "SELECT id,starts_at,ends_at,state,cursor_at,message,attempts,next_attempt_at,filled_seconds,bytes_fetched,observed_kbps FROM backfill_jobs WHERE camera_id=? ORDER BY starts_at DESC LIMIT 20",
            (camera_id,))]
    return {"enabled": bool(row["backfill_enabled"]), "limit_kbps": row["backfill_limit_kbps"], "default_limit_kbps": DEFAULT_LIMIT_KBPS,
            "support": support_view(camera_id), "jobs": jobs}
