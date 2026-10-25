"""Camera tamper detection: a covered, moved or defocused camera.

The recorder samples a small grayscale frame from each camera that has detection on, every few seconds, and compares
it with a *learned reference*: the average of the first ``LEARN_SAMPLES`` frames, then slowly adopting the scene as it
changes, so a day to night ramp is followed and never raises anything. A change is judged from three things that a
change of light alone cannot fake:

- **covered**: the frame has lost its contrast (a flat black cloth or a flat white sheet), or its brightness has
  collapsed against the reference, which is what a fast change in the light looks like to a scene that is very dark;
- **moved**: the picture's layout no longer matches the reference. Layout is the correlation of the average brightness
  of a grid of blocks, which is unchanged by brightening, darkening or fading the whole scene;
- **defocused**: the layout still matches but the edges have softened, measured by the average edge strength relative
  to the contrast, which is also unchanged by brightening or darkening.

A change must last ``sustain_seconds`` before it counts, and then raises a per-camera incident (``tamper_covered``,
``tamper_moved`` or ``tamper_defocused``) and fires any rule with the ``tamper`` condition. It recovers when the view
returns. After an intentional change, *re-learn the reference* starts the learning again and clears the incident.
Sensitivity (1 to 100, more is more sensitive) moves all the thresholds together.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .incidents import sync_incident
from .store import connect

logger = logging.getLogger("nvr.tamper")

FRAME_WIDTH, FRAME_HEIGHT = 160, 90
GRID_WIDTH, GRID_HEIGHT = 32, 18  # Blocks of 5 by 5 pixels.
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT
LEARN_SAMPLES = 5
ADOPT_RATE = 0.08  # How much of each unchanged sample the reference takes on.
CLEAR_SAMPLES = 2  # Consecutive unchanged samples that end a detection.
SAMPLE_SECONDS = float(os.environ.get("NVR_TAMPER_SAMPLE_SECONDS", "15"))
GRAB_TIMEOUT_SECONDS = 20
CONCURRENT_GRABS = 2
KINDS = ("covered", "moved", "defocused")
SUMMARY = {"covered": "Camera covered", "moved": "Camera moved", "defocused": "Camera defocused"}
# A reference this flat says nothing about what a change would look like, so these guards stay quiet for it.
MIN_REFERENCE_STD = 4.0
MIN_REFERENCE_MEAN = 25.0
MIN_REFERENCE_LAYOUT = 3.0
MIN_REFERENCE_SHARPNESS = 0.04


@dataclass(frozen=True)
class Features:
    mean: float
    std: float
    sharpness: float
    blocks: tuple[float, ...]

    def as_json(self) -> dict[str, Any]:
        return {"mean": self.mean, "std": self.std, "sharpness": self.sharpness, "blocks": [round(value, 3) for value in self.blocks]}

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "Features":
        return cls(float(value["mean"]), float(value["std"]), float(value["sharpness"]), tuple(float(item) for item in value["blocks"]))


def features(frame: bytes) -> Features:
    """What a frame (``FRAME_WIDTH`` by ``FRAME_HEIGHT`` grayscale bytes) is judged by."""
    if len(frame) != FRAME_BYTES:
        raise ValueError(f"Expected a {FRAME_BYTES}-byte grayscale frame")
    mean = sum(frame) / FRAME_BYTES
    std = math.sqrt(sum((value - mean) ** 2 for value in frame) / FRAME_BYTES)
    edges = 0
    for y in range(FRAME_HEIGHT):
        row = frame[y * FRAME_WIDTH:(y + 1) * FRAME_WIDTH]
        edges += sum(abs(a - b) for a, b in zip(row, row[1:]))
        if y + 1 < FRAME_HEIGHT:
            below = frame[(y + 1) * FRAME_WIDTH:(y + 2) * FRAME_WIDTH]
            edges += sum(abs(a - b) for a, b in zip(row, below))
    sharpness = (edges / (2 * FRAME_BYTES)) / max(std, 1.0)
    size_x, size_y = FRAME_WIDTH // GRID_WIDTH, FRAME_HEIGHT // GRID_HEIGHT
    blocks = []
    for by in range(GRID_HEIGHT):
        for bx in range(GRID_WIDTH):
            total = 0
            for y in range(by * size_y, (by + 1) * size_y):
                start = y * FRAME_WIDTH + bx * size_x
                total += sum(frame[start:start + size_x])
            blocks.append(total / (size_x * size_y))
    return Features(mean, std, sharpness, tuple(blocks))


def blend(reference: Features, sample: Features, rate: float) -> Features:
    def mix(old: float, new: float) -> float:
        return old + (new - old) * rate
    return Features(mix(reference.mean, sample.mean), mix(reference.std, sample.std), mix(reference.sharpness, sample.sharpness),
                    tuple(mix(old, new) for old, new in zip(reference.blocks, sample.blocks)))


def _spread(values: tuple[float, ...]) -> float:
    centre = sum(values) / len(values)
    return math.sqrt(sum((value - centre) ** 2 for value in values) / len(values))


def layout_match(reference: Features, sample: Features) -> float:
    """Pearson correlation of the block brightness patterns: 1 for the same layout, near 0 or below for a different one."""
    a, b = reference.blocks, sample.blocks
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    spread_a, spread_b = _spread(a), _spread(b)
    if spread_a < 1e-6 or spread_b < 1e-6:
        return 0.0
    return sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b)) / (len(a) * spread_a * spread_b)


@dataclass(frozen=True)
class Thresholds:
    covered_mean: float
    covered_std: float
    layout: float
    sharpness: float

    @classmethod
    def for_sensitivity(cls, sensitivity: int) -> "Thresholds":
        k = max(1, min(100, int(sensitivity))) / 100
        return cls(covered_mean=0.10 + 0.35 * k, covered_std=0.10 + 0.30 * k, layout=0.30 + 0.45 * k, sharpness=0.45 + 0.35 * k)


def judge(reference: Features, sample: Features, sensitivity: int) -> tuple[str | None, dict[str, float]]:
    """The kind of tampering the sample shows against the reference, if any, with the measurements behind it."""
    limit = Thresholds.for_sensitivity(sensitivity)
    layout = layout_match(reference, sample)
    metrics = {"brightness": round(sample.mean, 2), "reference_brightness": round(reference.mean, 2), "contrast": round(sample.std, 2),
               "reference_contrast": round(reference.std, 2), "layout_match": round(layout, 3), "sharpness": round(sample.sharpness, 4),
               "reference_sharpness": round(reference.sharpness, 4)}
    if (reference.std >= MIN_REFERENCE_STD and sample.std < limit.covered_std * reference.std) or \
            (reference.mean >= MIN_REFERENCE_MEAN and sample.mean < limit.covered_mean * reference.mean):
        return "covered", metrics
    if _spread(reference.blocks) >= MIN_REFERENCE_LAYOUT and layout < limit.layout:
        return "moved", metrics
    if reference.sharpness >= MIN_REFERENCE_SHARPNESS and sample.sharpness < limit.sharpness * reference.sharpness:
        return "defocused", metrics
    return None, metrics


# --- state, kept per camera ---

def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def settings(db: sqlite3.Connection, camera_id: str) -> sqlite3.Row:
    db.execute("INSERT OR IGNORE INTO tamper_settings(camera_id) VALUES(?)", (camera_id,))
    return db.execute("SELECT * FROM tamper_settings WHERE camera_id=?", (camera_id,)).fetchone()


def _camera_name(db: sqlite3.Connection, camera_id: str) -> str:
    row = db.execute("SELECT name FROM cameras WHERE id=?", (camera_id,)).fetchone()
    return row["name"] if row else camera_id


def _incident(db: sqlite3.Connection, camera_id: str, kind: str, active: bool, details: dict[str, Any]) -> None:
    sync_incident(db, incident_key=f"tamper:{camera_id}:{kind}", camera_id=camera_id, fault_type=f"tamper_{kind}",
                  summary=f"{SUMMARY[kind]}: {_camera_name(db, camera_id)}", active=active, details={"kind": kind, **details})


def _recover_all(db: sqlite3.Connection, camera_id: str, only: str | None = None) -> None:
    for kind in KINDS if only is None else (only,):
        _incident(db, camera_id, kind, False, {})


def process_sample(db: sqlite3.Connection, camera_id: str, frame: bytes, now: datetime | None = None) -> str:
    """Judge one sample, move the camera's state on, and raise or recover incidents. Returns the state afterwards.

    Runs in its own transaction so a settings change or a re-learn cannot interleave with it."""
    now = now or datetime.now(timezone.utc)
    sample = features(frame)
    db.execute("BEGIN IMMEDIATE")
    try:
        state = _advance(db, camera_id, sample, now)
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
    return state


def _advance(db: sqlite3.Connection, camera_id: str, sample: Features, now: datetime) -> str:
    row = settings(db, camera_id)
    changes: dict[str, Any] = {"last_sample_at": _stamp(now)}
    state = row["state"]
    reference = Features.from_json(json.loads(row["reference_json"])) if row["reference_json"] else None

    if reference is None or row["reference_samples"] < LEARN_SAMPLES:
        count = row["reference_samples"]
        reference = sample if reference is None else blend(reference, sample, 1 / (count + 1))  # A running average of the samples so far.
        count += 1
        changes.update(reference_json=json.dumps(reference.as_json()), reference_samples=count, candidate=None, candidate_since=None, clear_samples=0)
        if count >= LEARN_SAMPLES:
            changes.update(state="ok", state_since=_stamp(now), reference_at=_stamp(now))
            state = "ok"
        else:
            changes["state"] = state = "learning"
        return _save(db, camera_id, changes, state)

    kind, metrics = judge(reference, sample, row["sensitivity"])
    changes["last_metrics_json"] = json.dumps({**metrics, "judged": kind})
    sustain = timedelta(seconds=row["sustain_seconds"])

    if state in KINDS:
        if kind == state:
            changes["clear_samples"] = 0
        elif kind is None:
            clear = row["clear_samples"] + 1
            changes["clear_samples"] = clear
            if clear >= CLEAR_SAMPLES:
                _incident(db, camera_id, state, False, metrics)
                changes.update(state="ok", state_since=_stamp(now), clear_samples=0, candidate=None, candidate_since=None)
                state = "ok"
        else:
            _incident(db, camera_id, state, False, metrics)  # One kind of tampering gave way to another.
            changes.update(state="ok", state_since=_stamp(now), clear_samples=0, candidate=kind, candidate_since=_stamp(now))
            state = "ok"
        return _save(db, camera_id, changes, state)

    if kind is None:
        changes.update(candidate=None, candidate_since=None, reference_json=json.dumps(blend(reference, sample, ADOPT_RATE).as_json()))
    elif row["candidate"] == kind and row["candidate_since"] and now - _parse(row["candidate_since"]) >= sustain:
        changes.update(state=kind, state_since=_stamp(now), candidate=None, candidate_since=None, clear_samples=0)
        state = kind
        _incident(db, camera_id, kind, True, metrics)
        from .rules import fire_tamper
        fire_tamper(db, camera_id, kind, now)
    elif row["candidate"] != kind:
        changes.update(candidate=kind, candidate_since=_stamp(now))
    return _save(db, camera_id, changes, state)


def _save(db: sqlite3.Connection, camera_id: str, changes: dict[str, Any], state: str) -> str:
    changes["updated_at"] = _stamp(datetime.now(timezone.utc))
    db.execute(f"UPDATE tamper_settings SET {','.join(f'{column}=?' for column in changes)} WHERE camera_id=?", (*changes.values(), camera_id))
    return state


def relearn(db: sqlite3.Connection, camera_id: str) -> None:
    """Forget the reference, as after a deliberate change: the view is learned again and any detection ends."""
    db.execute("BEGIN IMMEDIATE")
    try:
        settings(db, camera_id)
        _recover_all(db, camera_id)
        db.execute("UPDATE tamper_settings SET reference_json=NULL,reference_samples=0,reference_at=NULL,state='learning',state_since=NULL,candidate=NULL,"
                   "candidate_since=NULL,clear_samples=0,last_metrics_json=NULL,updated_at=? WHERE camera_id=?", (_stamp(datetime.now(timezone.utc)), camera_id))
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def configure(db: sqlite3.Connection, camera_id: str, *, enabled: bool, sensitivity: int, sustain_seconds: int) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        row = settings(db, camera_id)
        if not enabled:
            _recover_all(db, camera_id)
        changes = {"enabled": int(enabled), "sensitivity": sensitivity, "sustain_seconds": sustain_seconds, "updated_at": _stamp(datetime.now(timezone.utc))}
        if not enabled or row["state"] in KINDS:
            changes.update(state="ok" if row["reference_json"] and row["reference_samples"] >= LEARN_SAMPLES else "learning", candidate=None, candidate_since=None, clear_samples=0)
        db.execute(f"UPDATE tamper_settings SET {','.join(f'{column}=?' for column in changes)} WHERE camera_id=?", (*changes.values(), camera_id))
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def view(db: sqlite3.Connection, camera_id: str) -> dict[str, Any]:
    row = settings(db, camera_id)
    return {"enabled": bool(row["enabled"]), "sensitivity": row["sensitivity"], "sustain_seconds": row["sustain_seconds"], "state": row["state"] if row["enabled"] else "off",
            "state_since": row["state_since"], "learning_progress": min(row["reference_samples"], LEARN_SAMPLES), "learning_samples": LEARN_SAMPLES,
            "reference_at": row["reference_at"], "last_sample_at": row["last_sample_at"],
            "last_metrics": json.loads(row["last_metrics_json"]) if row["last_metrics_json"] else None, "sample_seconds": SAMPLE_SECONDS}


# --- sampling ---

Grabber = Callable[[str], Awaitable["bytes | None"]]


async def grab_frame(source: str, protocols: str | None = None) -> bytes | None:
    """One grayscale frame, scaled down, from a stream; None when it could not be had in time.

    Only camera protocols are allowed unless the caller widens them (the tests read generated files, as ``file:`` URLs)."""
    from .recording import _PROTOCOLS, _manifest_bytes
    executable = shutil.which("ffmpeg")
    if not executable:
        return None
    args = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-protocol_whitelist", protocols or _PROTOCOLS, "-i", "pipe:0",
            "-an", "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=area,format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(_manifest_bytes(source)), timeout=GRAB_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, OSError):
        return None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return output if len(output) == FRAME_BYTES else None


async def grab_camera_frame(camera_id: str) -> bytes | None:
    """A frame from the camera's low-resolution stream, or its main one when it has none."""
    from .credentials import decrypt
    from .recording import safe_stream_url
    with connect() as db:
        row = db.execute("SELECT stream_url_ciphertext,live_substream_url_ciphertext FROM cameras WHERE id=? AND enabled=1 AND archived_at IS NULL", (camera_id,)).fetchone()
    if row is None:
        return None
    try:
        source = safe_stream_url(decrypt(row["live_substream_url_ciphertext"] or row["stream_url_ciphertext"]))
    except (RuntimeError, ValueError):
        return None
    return await grab_frame(source)


def _eligible(db: sqlite3.Connection) -> list[str]:
    return [row["camera_id"] for row in db.execute(
        "SELECT t.camera_id FROM tamper_settings t JOIN cameras c ON c.id=t.camera_id WHERE t.enabled=1 AND c.enabled=1 AND c.archived_at IS NULL ORDER BY t.camera_id")]


def _release_ineligible(db: sqlite3.Connection) -> None:
    """A camera that was archived, disabled or switched off while a detection was open ends it."""
    rows = db.execute("SELECT t.camera_id,t.state FROM tamper_settings t LEFT JOIN cameras c ON c.id=t.camera_id "
                      "WHERE t.state IN ('covered','moved','defocused') AND (t.enabled=0 OR c.id IS NULL OR c.enabled=0 OR c.archived_at IS NOT NULL)").fetchall()
    for row in rows:
        db.execute("BEGIN IMMEDIATE")
        try:
            _recover_all(db, row["camera_id"])
            db.execute("UPDATE tamper_settings SET state='ok',candidate=NULL,candidate_since=NULL,clear_samples=0 WHERE camera_id=?", (row["camera_id"],))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


class TamperManager:
    """Samples every camera with detection on, a few at a time, in a task of its own: nothing here waits on or is waited on by recording."""

    def __init__(self, grabber: Grabber = grab_camera_frame, interval: float | None = None) -> None:
        self.grabber = grabber
        self.interval = SAMPLE_SECONDS if interval is None else interval
        self.task: asyncio.Task[None] | None = None
        self.stopping = False

    async def start(self) -> None:
        self.stopping = False
        self.task = asyncio.create_task(self._run(), name="tamper-detection")

    async def stop(self) -> None:
        self.stopping = True
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _run(self) -> None:
        while not self.stopping:
            try:
                await self.sample_all()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - detection must never end because one pass failed
                logger.exception("A tamper detection pass failed")
            await asyncio.sleep(self.interval)

    async def sample_all(self, now: datetime | None = None) -> int:
        def read() -> list[str]:
            with connect() as db:
                _release_ineligible(db)
                return _eligible(db)
        cameras = await asyncio.to_thread(read)
        slots = asyncio.Semaphore(CONCURRENT_GRABS)

        async def one(camera_id: str) -> bool:
            async with slots:
                try:
                    frame = await self.grabber(camera_id)
                    if frame is None:
                        return False  # No picture is the recorder's fault to report, not a tamper.
                    await asyncio.to_thread(self._judge, camera_id, frame, now)
                    return True
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one camera's trouble must not stop the others
                    logger.exception("Tamper detection failed for camera %s", camera_id)
                    return False
        return sum(await asyncio.gather(*(one(camera_id) for camera_id in cameras)))

    @staticmethod
    def _judge(camera_id: str, frame: bytes, now: datetime | None) -> None:
        with connect() as db:
            process_sample(db, camera_id, frame, now)
