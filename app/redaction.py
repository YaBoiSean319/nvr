"""Export redaction (#130): blur faces and licence plates in an exported video.

A *review* comes first. The administrator picks cameras, a range and what to blur; the NVR decodes that range exactly as the export will
(same size, same timeline), samples it a few times a second, finds faces and plates with OpenVINO models, and links the detections into
tracks. The reviewer then sees every track, can stop one being blurred (the subject of a request) or add a manual region that follows a
detected track or moves between keyframes, and confirms. Only a confirmed review can be attached to an export; the export blurs those
regions in the rendered clips, never keeps an unblurred copy, and records the review, who made it and the settings in its manifest. The
recordings themselves are never touched.

The models are chosen by the administrator, like the search models: ``NVR_AI_REDACTION_MODEL_DIR`` holds ``face.xml/.bin`` and/or
``plate.xml/.bin``. Both are SSD-style OpenVINO detectors whose output is rows of ``[image, label, confidence, x1, y1, x2, y2]``
(Open Model Zoo's ``face-detection-0200`` and ``vehicle-license-plate-detection-barrier-0106`` are of this kind; the plate model reports
vehicles as label 1 and plates as label 2, set with ``NVR_AI_PLATE_LABEL``). Everything runs in this process.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from . import privacy, viewing_audit
from .detection import box_iou
from .recording import recordings_path
from .retention import acquire_playback, release_playback
from .storage import recording_file_path
from .store import connect

MODEL_DIR = Path(os.environ.get("NVR_AI_REDACTION_MODEL_DIR", "/opt/nvr/models/redaction"))
try:
    PLATE_LABEL = int(os.environ.get("NVR_AI_PLATE_LABEL", "2"))
except ValueError:
    PLATE_LABEL = 2
KINDS = {"faces": "face", "plates": "plate"}
FRAME_WIDTH, FRAME_HEIGHT = 1280, 720
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * 3
SAMPLE_FPS = 5
OUTPUT_FPS = 15
THRESHOLD = 0.4
PAD = 0.2  # Each blurred box is grown by this fraction of its size on every side, and by at least MIN_PAD_PX
MIN_PAD_PX = 6
BLOCKS = 5  # A blurred box is reduced to about this many blocks across its longer side: nothing readable survives
HOLD = 0.4  # A box keeps covering its subject this long (seconds) before its first and after its last sighting
MAX_GAP = 1.0  # Detections further apart than this (seconds) are different tracks
MAX_TRACKS_PER_CAMERA = 2000
MAX_MANUAL = 50
MAX_KEYFRAMES = 100
MAX_CAMERA_SECONDS = 2 * 3600  # Cameras times seconds of footage one review may cover
MAX_ACTIVE_REVIEWS = 2
REVIEW_TTL = timedelta(hours=24)
FOLLOW_SCALES = (0.25, 4.0)

log = logging.getLogger(__name__)


class RedactionError(ValueError):
    """The review cannot be made or changed as asked."""


class Cancelled(Exception):
    """The export was cancelled while its clip was being redacted."""


# ---------------------------------------------------------------------------------------------------------------- detection

class SsdModel:
    """One OpenVINO SSD detector and the one kind of object it finds."""

    def __init__(self, run: Callable[[np.ndarray], Any], size: tuple[int, int], kind: str, label: int | None = None) -> None:
        self.run, self.size, self.kind, self.label = run, size, kind, label
        self._lock = threading.Lock()  # One inference request at a time

    def detect(self, frame: np.ndarray, threshold: float) -> list[dict[str, Any]]:
        height, width = self.size
        rows = np.minimum((np.arange(height) * frame.shape[0] / height).astype(int), frame.shape[0] - 1)
        columns = np.minimum((np.arange(width) * frame.shape[1] / width).astype(int), frame.shape[1] - 1)
        tensor = frame[rows][:, columns].astype(np.float32).transpose(2, 0, 1)[None]  # BGR, 0-255: the Open Model Zoo input contract
        with self._lock:
            result = self.run(tensor)
        values = list(result.values()) if hasattr(result, "values") else [result]
        found = []
        for _, label, score, x1, y1, x2, y2 in np.asarray(values[0], dtype=np.float32).reshape(-1, 7):
            if not np.isfinite(score) or score < threshold or (self.label is not None and int(label) != self.label):
                continue
            x1, y1, x2, y2 = (max(0.0, min(1.0, float(value))) for value in (x1, y1, x2, y2))
            if x2 > x1 and y2 > y1:
                found.append({"kind": self.kind, "confidence": float(score), "box": {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}})
        return found


class RedactionDetector:
    def __init__(self, models: dict[str, SsdModel], model_id: str) -> None:
        self.models, self.model_id = models, model_id

    def kinds(self) -> set[str]:
        return set(self.models)

    def detect(self, frame: np.ndarray, kinds: Iterable[str], threshold: float = THRESHOLD) -> list[dict[str, Any]]:
        return [item for kind in kinds if kind in self.models for item in self.models[kind].detect(frame, threshold)]


def load_detector(path: Path = MODEL_DIR, provider: str = "CPU", core: Any = None) -> RedactionDetector | None:
    """The detectors whose files are in ``path`` and load, or None when there are none (a redaction review then cannot be made)."""
    models: dict[str, SsdModel] = {}
    try:
        for kind, label in (("face", None), ("plate", PLATE_LABEL)):
            if not all((path / f"{kind}.{extension}").is_file() for extension in ("xml", "bin")):
                continue
            if core is None:
                import openvino as ov
                core = ov.Core()
            compiled = core.compile_model(core.read_model(str(path / f"{kind}.xml")), provider)
            shape = [int(value) for value in compiled.input(0).shape]
            models[kind] = SsdModel(compiled, (shape[2], shape[3]), kind, label)
    except Exception:  # noqa: BLE001 - a model that will not load only means that kind cannot be redacted
        log.exception("A redaction model could not be loaded")
    return RedactionDetector(models, f"redaction:{path.name}") if models else None


_shared: dict[str, RedactionDetector | None] = {}
_shared_lock = threading.Lock()


def shared_detector() -> RedactionDetector | None:
    key = str(MODEL_DIR)
    with _shared_lock:
        if key not in _shared:
            _shared[key] = load_detector(MODEL_DIR)
        return _shared[key]


def available_kinds() -> list[str]:
    detector = shared_detector()
    return sorted(word for word, kind in KINDS.items() if detector is not None and kind in detector.kinds())


# -------------------------------------------------------------------------------------------------------------------- tracks

def link(frames: Iterable[tuple[float, list[dict[str, Any]]]], camera_id: str) -> list[dict[str, Any]]:
    """Link each sampled moment's detections into tracks by overlap, so one face or plate has one track while it moves."""
    tracks: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []
    for at, found in frames:
        active = [track for track in active if at - track["end"] <= MAX_GAP]
        taken: set[str] = set()
        for item in sorted(found, key=lambda entry: -entry["confidence"]):
            box = item["box"]
            best, best_overlap = None, 0.0
            for track in active:
                if track["kind"] != item["kind"] or track["id"] in taken:
                    continue
                last = track["samples"][-1]
                overlap = box_iou({"x": last[1], "y": last[2], "width": last[3], "height": last[4]}, box)
                if overlap > best_overlap:
                    best, best_overlap = track, overlap
            sample = [round(at, 3), round(box["x"], 4), round(box["y"], 4), round(box["width"], 4), round(box["height"], 4)]
            if best is not None and best_overlap >= 0.1:
                best["samples"].append(sample)
                best["end"] = round(at, 3)
                best["confidence"] = max(best["confidence"], round(item["confidence"], 3))
                taken.add(best["id"])
            elif len(tracks) < MAX_TRACKS_PER_CAMERA:
                track = {"id": f"t-{uuid.uuid4().hex[:10]}", "camera_id": camera_id, "kind": item["kind"], "start": round(at, 3), "end": round(at, 3),
                         "confidence": round(item["confidence"], 3), "samples": [sample], "blur": True}
                tracks.append(track)
                active.append(track)
                taken.add(track["id"])
    return tracks


def _interpolate(samples: list[list[float]], at: float, hold: float) -> tuple[float, float, float, float] | None:
    """The box at ``at`` between sightings, or the nearest sighting within ``hold`` seconds of either end."""
    if not samples or at < samples[0][0] - hold or at > samples[-1][0] + hold:
        return None
    if at <= samples[0][0]:
        return tuple(samples[0][1:5])  # type: ignore[return-value]
    if at >= samples[-1][0]:
        return tuple(samples[-1][1:5])  # type: ignore[return-value]
    for left, right in zip(samples, samples[1:]):
        if left[0] <= at <= right[0]:
            weight = 0.0 if right[0] == left[0] else (at - left[0]) / (right[0] - left[0])
            return tuple(left[index] + (right[index] - left[index]) * weight for index in range(1, 5))  # type: ignore[return-value]
    return None


def _manual_box(region: dict[str, Any], tracks: dict[str, dict[str, Any]], at: float) -> tuple[float, float, float, float] | None:
    if at < region["start"] or at > region["end"]:
        return None
    followed = tracks.get(region.get("follow_track_id") or "")
    if followed is not None:
        box = _interpolate(followed["samples"], at, HOLD)
        if box is None:
            return None
        scale = float(region.get("scale") or 1.0)
        x, y, width, height = box
        return (x + width / 2 - width * scale / 2, y + height / 2 - height * scale / 2, width * scale, height * scale)
    keyframes = [[frame["t"], frame["x"], frame["y"], frame["width"], frame["height"]] for frame in region["keyframes"]]
    return _interpolate(keyframes, at, float("inf"))


def boxes_at(regions: dict[str, Any], camera_id: str, at: float) -> list[tuple[float, float, float, float]]:
    """Every box to blur in this camera's clip at ``at`` seconds: its tracks that are still blurred, and the manual regions."""
    tracks = {track["id"]: track for track in regions.get("tracks", []) if track["camera_id"] == camera_id}
    found = [box for track in tracks.values() if track["blur"] and (box := _interpolate(track["samples"], at, HOLD)) is not None]
    found.extend(box for region in regions.get("manual", []) if region["camera_id"] == camera_id and (box := _manual_box(region, tracks, at)) is not None)
    return found


# ------------------------------------------------------------------------------------------------------------------ pixels

def _pixel_box(box: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    x, y, width, height = box
    pad_x, pad_y = max(PAD * width * FRAME_WIDTH, MIN_PAD_PX), max(PAD * height * FRAME_HEIGHT, MIN_PAD_PX)
    return (max(0, int(x * FRAME_WIDTH - pad_x)), max(0, int(y * FRAME_HEIGHT - pad_y)),
            min(FRAME_WIDTH, int(np.ceil((x + width) * FRAME_WIDTH + pad_x))), min(FRAME_HEIGHT, int(np.ceil((y + height) * FRAME_HEIGHT + pad_y))))


def redact_frame(image: np.ndarray, boxes: Iterable[tuple[float, float, float, float]]) -> int:
    """Pixelate each box of a BGR frame in place and say how many boxes covered any pixels."""
    done = 0
    for box in boxes:
        x1, y1, x2, y2 = _pixel_box(box)
        if x2 <= x1 or y2 <= y1:
            continue
        region = image[y1:y2, x1:x2]
        block = max(4, int(np.ceil(max(x2 - x1, y2 - y1) / BLOCKS)))
        for top in range(0, y2 - y1, block):
            for left in range(0, x2 - x1, block):
                cell = region[top:top + block, left:left + block]
                cell[...] = cell.reshape(-1, 3).mean(axis=0).astype(np.uint8)
        done += 1
    return done


def _outline(image: np.ndarray, box: tuple[float, float, float, float], colour: tuple[int, int, int]) -> None:
    x1, y1, x2, y2 = _pixel_box(box)
    for edge in (slice(y1, min(y1 + 3, y2)), slice(max(y2 - 3, y1), y2)):
        image[edge, x1:x2] = colour
    for edge in (slice(x1, min(x1 + 3, x2)), slice(max(x2 - 3, x1), x2)):
        image[y1:y2, edge] = colour


def _ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("FFmpeg is unavailable")
    return executable


def _decode_filter(zones: list[dict[str, float]], fps: float | None) -> str:
    chain = ([f"fps={fps}"] if fps else []) + [f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:force_original_aspect_ratio=decrease", f"pad={FRAME_WIDTH}:{FRAME_HEIGHT}:(ow-iw)/2:(oh-ih)/2", "setsar=1"]
    return privacy.with_mask(",".join(chain), zones)


def redact_clip(source: Path, target: Path, regions: dict[str, Any], camera_id: str, cancelled: Callable[[], bool]) -> dict[str, int]:
    """Re-encode a rendered clip with its boxes blurred, frame by frame. Returns the frame and blurred-box counts; raises on any failure."""
    decoder = subprocess.Popen([_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source), "-map", "0:v:0", "-an",
                                "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{FRAME_WIDTH}x{FRAME_HEIGHT}", "pipe:1"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    encoder = subprocess.Popen([_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                                "-s", f"{FRAME_WIDTH}x{FRAME_HEIGHT}", "-r", str(OUTPUT_FPS), "-i", "pipe:0", "-c:v", "libx264", "-preset", "ultrafast",
                                "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y", str(target)],
                               stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frames = blurred = 0
    assert decoder.stdout is not None and encoder.stdin is not None
    try:
        while True:
            if cancelled():
                raise Cancelled()
            data = decoder.stdout.read(FRAME_BYTES)
            if len(data) < FRAME_BYTES:
                break
            image = np.frombuffer(data, dtype=np.uint8).reshape(FRAME_HEIGHT, FRAME_WIDTH, 3).copy()
            blurred += redact_frame(image, boxes_at(regions, camera_id, frames / OUTPUT_FPS))
            encoder.stdin.write(image.tobytes())
            frames += 1
        encoder.stdin.close()
        if encoder.wait() != 0 or decoder.wait() != 0 or not frames:
            raise RuntimeError("The clip could not be redacted")
    except BaseException:
        for process in (decoder, encoder):
            if process.poll() is None:
                process.kill()
        target.unlink(missing_ok=True)
        raise
    return {"frames": frames, "boxes": blurred}


# ------------------------------------------------------------------------------------------------------------------ reviews

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _regions(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["regions_json"]) if row["regions_json"] else {}


def view(row: sqlite3.Row) -> dict[str, Any]:
    """A review as a person sees it: tracks without their per-sighting boxes."""
    regions = _regions(row)
    return {
        "id": row["id"], "state": row["state"], "progress": int(row["progress"]), "error": row["error"],
        "camera_ids": json.loads(row["camera_ids_json"]), "start": row["requested_start"], "end": row["requested_end"],
        "kinds": json.loads(row["kinds_json"]), "created_at": row["created_at"], "edited_at": row["edited_at"], "confirmed_at": row["confirmed_at"],
        "export_id": row["export_id"], "durations": regions.get("durations", {}),
        "tracks": [{key: track[key] for key in ("id", "camera_id", "kind", "start", "end", "confidence", "blur")} | {"box": dict(zip(("x", "y", "width", "height"), track["samples"][len(track["samples"]) // 2][1:5]))}
                   for track in regions.get("tracks", [])],
        "manual": regions.get("manual", []),
    }


def get_review(review_id: str, requester_id: int, is_admin: bool = False) -> sqlite3.Row | None:
    with connect() as db:
        if is_admin:
            return db.execute("SELECT * FROM redaction_reviews WHERE id=?", (review_id,)).fetchone()
        return db.execute("SELECT * FROM redaction_reviews WHERE id=? AND requested_by=?", (review_id, requester_id)).fetchone()


def create_review(requested_by: int, camera_ids: list[str], start: datetime, end: datetime, kinds: list[str]) -> str:
    from .exports import _parse_time, _unique_output_spans  # noqa: F401 - the review samples the same footage an export would render
    wanted = [word for word in kinds if word in KINDS]
    if not wanted or len(set(wanted)) != len(wanted) or len(wanted) != len(kinds):
        raise ValueError("Choose faces, plates or both")
    missing = [word for word in wanted if word not in available_kinds()]
    if missing:
        raise ValueError(f"The {' and '.join(missing)} detection model is not installed (see NVR_AI_REDACTION_MODEL_DIR)")
    if len(camera_ids) * (end - start).total_seconds() > MAX_CAMERA_SECONDS:
        raise ValueError("A redaction review covers at most two hours of footage in total (for example four cameras for thirty minutes)")
    review_id = str(uuid.uuid4())
    now = _now().isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT COUNT(*) FROM redaction_reviews WHERE state IN ('queued','analysing')").fetchone()[0] >= MAX_ACTIVE_REVIEWS:
            raise OverflowError("Redaction reviews are busy; wait for one to finish")
        placeholders = ",".join("?" for _ in camera_ids)
        if {row[0] for row in db.execute(f"SELECT id FROM cameras WHERE id IN ({placeholders})", tuple(camera_ids))} != set(camera_ids):
            raise LookupError("Redaction request contains an unknown camera")
        if not db.execute(f"SELECT 1 FROM recording_segments WHERE camera_id IN ({placeholders}) AND starts_at<? AND ends_at>? AND unavailable_at IS NULL LIMIT 1",
                          (*camera_ids, end.isoformat(), start.isoformat())).fetchone():
            raise LookupError("No indexed footage is available in the requested interval")
        db.execute("INSERT INTO redaction_reviews(id,requested_by,camera_ids_json,requested_start,requested_end,kinds_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (review_id, requested_by, json.dumps(camera_ids), start.isoformat(), end.isoformat(), json.dumps(wanted), "queued", now, now))
        viewing_audit.record_events(db, requested_by, camera_ids, "export", review_id)
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (requested_by, "redaction_review_requested", "redaction_review", review_id,
                    json.dumps({"cameras": len(camera_ids), "start": start.isoformat(), "end": end.isoformat(), "kinds": wanted})))
    return review_id


def _sources(db: sqlite3.Connection, camera_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    """A camera's footage in the range as an export would use it: the playable spans, and any privacy zones."""
    from .exports import _parse_time, _unique_output_spans
    segments = [dict(item) for item in db.execute(
        "SELECT id,relative_path,starts_at,ends_at,size_bytes FROM recording_segments WHERE camera_id=? AND starts_at<? AND ends_at>? AND unavailable_at IS NULL ORDER BY starts_at",
        (camera_id, end.isoformat(), start.isoformat())).fetchall()]
    zones: list[dict[str, float]] = []
    for segment in segments:
        zones = zones or privacy.server_zones(db, camera_id, _parse_time(segment["starts_at"]))
    return {"spans": _unique_output_spans(start, end, segments), "zones": zones}


def _concat_text(spans: list[dict[str, Any]]) -> str:
    from .exports import _ffconcat_quote, _parse_time
    lines = ["ffconcat version 1.0\n"]
    for span in spans:
        segment = span["segment"]
        lines.append(_ffconcat_quote(recording_file_path(segment["relative_path"])))
        lines.append(f"inpoint {(span['source_start'] - _parse_time(segment['starts_at'])).total_seconds():.6f}\n")
        lines.append(f"outpoint {(span['source_end'] - _parse_time(segment['starts_at'])).total_seconds():.6f}\n")
    return "".join(lines)


def _lease(spans: list[dict[str, Any]]) -> list[Path]:
    """Keep these recordings from being deleted while they are read. Retention takes the same write lock before unlinking."""
    held: list[Path] = []
    for span in spans:
        path = recording_file_path(span["segment"]["relative_path"])
        if not path.is_file():
            for item in held:
                release_playback(item)
            raise RuntimeError("A source recording is unavailable")
        acquire_playback(path)
        held.append(path)
    return held


def fingerprint(plans: Iterable[tuple[str, dict[str, Any]]]) -> str:
    """Which footage (and which privacy zones) a review's boxes belong to. Boxes are keyed to the clip's timeline, so any change to either makes them wrong."""
    import hashlib
    return hashlib.sha256(json.dumps(
        [[camera_id, [[int(span["segment"]["id"]), span["source_start"].isoformat(), span["source_end"].isoformat()] for span in plan["spans"]], plan.get("zones", plan.get("privacy_zones", []))]
         for camera_id, plan in sorted(plans, key=lambda item: item[0])], sort_keys=True).encode()).hexdigest()


def _set(review_id: str, **values: Any) -> None:
    values["updated_at"] = _now().isoformat()
    with connect() as db:
        db.execute(f"UPDATE redaction_reviews SET {','.join(f'{name}=?' for name in values)} WHERE id=?", (*values.values(), review_id))


async def _frames(manifest: Path, zones: list[dict[str, float]]):
    process = await asyncio.create_subprocess_exec(
        _ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(manifest), "-map", "0:v:0", "-an",
        "-vf", _decode_filter(zones, SAMPLE_FPS), "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    assert process.stdout is not None
    try:
        while True:
            yield await process.stdout.readexactly(FRAME_BYTES)
    except asyncio.IncompleteReadError:
        pass
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def analyse(review_id: str, detector: RedactionDetector | None = None) -> None:
    """Find and link the faces and plates of one queued review. Footage is read through leases and nothing of it is kept."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM redaction_reviews WHERE id=? AND state='queued'", (review_id,)).fetchone()
        if row is None:
            return
        db.execute("UPDATE redaction_reviews SET state='analysing',progress=1,updated_at=? WHERE id=?", (_now().isoformat(), review_id))
    camera_ids, kinds = json.loads(row["camera_ids_json"]), [KINDS[word] for word in json.loads(row["kinds_json"])]
    start, end = _parse(row["requested_start"]), _parse(row["requested_end"])
    held: list[Path] = []
    work = recordings_path() / ".redaction" / review_id
    try:
        detector = detector or shared_detector()
        if detector is None:
            raise RuntimeError("The redaction models are not installed")
        work.mkdir(parents=True, exist_ok=True)
        tracks: list[dict[str, Any]] = []
        durations: dict[str, float] = {}
        plans = []
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for camera_id in camera_ids:
                plan = _sources(db, camera_id, start, end)
                if plan["spans"]:
                    held.extend(_lease(plan["spans"]))
                    plans.append((camera_id, plan))
        for index, (camera_id, plan) in enumerate(plans):
            manifest = work / f"{index}.ffconcat"
            manifest.write_text(_concat_text(plan["spans"]), encoding="utf-8")
            durations[camera_id] = round(sum(span["output_end"] - span["output_start"] for span in plan["spans"]), 3)
            samples: list[tuple[float, list[dict[str, Any]]]] = []
            number = 0
            async for data in _frames(manifest, plan["zones"]):
                image = np.frombuffer(data, dtype=np.uint8).reshape(FRAME_HEIGHT, FRAME_WIDTH, 3)
                samples.append((number / SAMPLE_FPS, await asyncio.to_thread(detector.detect, image, kinds)))
                number += 1
                if number % 25 == 0:
                    _set(review_id, progress=min(95, 1 + int(94 * (index + min(1.0, number / SAMPLE_FPS / max(durations[camera_id], 1))) / len(plans))))
            tracks.extend(link(samples, camera_id))
        _set(review_id, state="ready", progress=100, error=None, regions_json=json.dumps({"tracks": tracks, "manual": [], "durations": durations, "fingerprint": fingerprint(plans)}, separators=(",", ":")),
             models_json=json.dumps({"model": detector.model_id, "kinds": sorted(detector.kinds()), "threshold": THRESHOLD, "sample_fps": SAMPLE_FPS}))
    except asyncio.CancelledError:
        _set(review_id, state="failed", error="Interrupted by application shutdown")
        raise
    except Exception as exc:  # noqa: BLE001 - the review fails with a reason the person can read
        log.exception("Redaction review %s failed", review_id)
        _set(review_id, state="failed", error=str(exc)[:300] or "The review failed")
    finally:
        for path in held:
            release_playback(path)
        shutil.rmtree(work, ignore_errors=True)


async def advance_reviews() -> None:
    with connect() as db:
        queued = [row[0] for row in db.execute("SELECT id FROM redaction_reviews WHERE state='queued' ORDER BY created_at")]
    for review_id in queued:
        await analyse(review_id)


def recover() -> None:
    """A review the application stopped in the middle of cannot be resumed; its person can ask again."""
    with connect() as db:
        db.execute("UPDATE redaction_reviews SET state='failed',error='Interrupted by application restart',updated_at=? WHERE state='analysing'", (_now().isoformat(),))


def expire_reviews(now: datetime | None = None) -> None:
    """Reviews nobody used are forgotten after a day; one attached to an export stays with it."""
    cutoff = ((now or _now()) - REVIEW_TTL).isoformat()
    with connect() as db:
        db.execute("DELETE FROM redaction_reviews WHERE state NOT IN ('used','queued','analysing') AND updated_at<?", (cutoff,))
        # A used review stays for a retry only while its export can still be used; its boxes are not kept longer than that.
        db.execute("DELETE FROM redaction_reviews WHERE state='used' AND updated_at<? AND NOT EXISTS ("
                   "SELECT 1 FROM exports WHERE exports.id=redaction_reviews.export_id AND ("
                   "exports.status IN ('queued','running') OR (exports.status='completed' AND exports.expires_at>?)))", (cutoff, (now or _now()).isoformat()))


# --------------------------------------------------------------------------------------------------------------------- edits

def _box(value: dict[str, Any]) -> dict[str, float]:
    try:
        box = {name: round(float(value[name]), 4) for name in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError) as exc:
        raise RedactionError("A region needs x, y, width and height") from exc
    if not all(0.0 <= box[name] <= 1.0 for name in box) or box["width"] <= 0 or box["height"] <= 0 or box["x"] + box["width"] > 1.0001 or box["y"] + box["height"] > 1.0001:
        raise RedactionError("A region must lie inside the picture")
    return box


def _manual(spec: dict[str, Any], review: dict[str, Any], tracks: dict[str, dict[str, Any]], number: int) -> dict[str, Any]:
    camera_id = spec.get("camera_id")
    if camera_id not in review["camera_ids"] or camera_id not in review["durations"]:
        raise RedactionError("A manual region must be on one of the review's cameras")
    start, end = float(spec["start"]), float(spec["end"])
    if not 0 <= start < end <= review["durations"][camera_id] + 0.001:
        raise RedactionError("A manual region must lie inside the camera's clip")
    follow = spec.get("follow_track_id")
    region: dict[str, Any] = {"id": f"m-{number}", "camera_id": camera_id, "start": round(start, 3), "end": round(end, 3)}
    if follow:
        if follow not in tracks or tracks[follow]["camera_id"] != camera_id:
            raise RedactionError("A region can follow only a track on its own camera")
        scale = float(spec.get("scale") or 1.0)
        if not FOLLOW_SCALES[0] <= scale <= FOLLOW_SCALES[1]:
            raise RedactionError("A followed region may be scaled between a quarter and four times its track")
        return region | {"follow_track_id": follow, "scale": round(scale, 3), "keyframes": []}
    keyframes = sorted(({"t": round(float(frame["t"]), 3), **_box(frame)} for frame in spec.get("keyframes") or []), key=lambda frame: frame["t"])
    if not keyframes or len(keyframes) > MAX_KEYFRAMES:
        raise RedactionError("A region needs a track to follow or between 1 and 100 keyframes")
    return region | {"follow_track_id": None, "scale": 1.0, "keyframes": keyframes}


def update_review(row: sqlite3.Row, editor_id: int, keep: list[str] | None, manual: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Set which tracks stay unblurred and the manual regions. Either is replaced as a whole; both are checked before anything changes."""
    if row["state"] != "ready":
        raise PermissionError("Only a finished review that is not yet confirmed can be edited")
    regions = _regions(row)
    tracks = {track["id"]: track for track in regions["tracks"]}
    if keep is not None:
        if any(track_id not in tracks for track_id in keep):
            raise RedactionError("Unknown track")
        kept = set(keep)
        for track_id, track in tracks.items():
            track["blur"] = track_id not in kept
    if manual is not None:
        if len(manual) > MAX_MANUAL:
            raise RedactionError("A review holds at most 50 manual regions")
        context = view(row)
        regions["manual"] = [_manual(spec, context, tracks, number) for number, spec in enumerate(manual, 1)]
    now = _now().isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute("UPDATE redaction_reviews SET regions_json=?,edited_by=?,edited_at=?,updated_at=? WHERE id=? AND state='ready'",
                             (json.dumps(regions, separators=(",", ":")), editor_id, now, now, row["id"]))
        if changed.rowcount != 1:
            raise PermissionError("Only a finished review that is not yet confirmed can be edited")
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (editor_id, "redaction_review_edited", "redaction_review", row["id"],
                    json.dumps({"kept_tracks": sorted(track_id for track_id, track in tracks.items() if not track["blur"]), "manual_regions": len(regions["manual"])})))
        updated = db.execute("SELECT * FROM redaction_reviews WHERE id=?", (row["id"],)).fetchone()
    return view(updated)


def confirm_review(row: sqlite3.Row, confirmer_id: int) -> dict[str, Any]:
    now = _now().isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute("UPDATE redaction_reviews SET state='confirmed',confirmed_by=?,confirmed_at=?,updated_at=? WHERE id=? AND state='ready'", (confirmer_id, now, now, row["id"]))
        if changed.rowcount != 1:
            raise PermissionError("Only a finished review can be confirmed")
        db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                   (confirmer_id, "redaction_review_confirmed", "redaction_review", row["id"], "{}"))
        updated = db.execute("SELECT * FROM redaction_reviews WHERE id=?", (row["id"],)).fetchone()
    return view(updated)


def claim_for_export(db: sqlite3.Connection, review_id: str, requester_id: int, export_id: str, camera_ids: list[str], start: datetime, end: datetime,
                     output_modes: list[str], retry_of: str | None = None) -> None:
    """Attach a confirmed review to a new export (inside that export's transaction), or raise why it cannot be."""
    row = db.execute("SELECT * FROM redaction_reviews WHERE id=? AND requested_by=?", (review_id, requester_id)).fetchone()
    if row is None:
        raise LookupError("Redaction review not found")
    reusable = retry_of is not None and row["state"] == "used" and row["export_id"] == retry_of
    if row["state"] != "confirmed" and not reusable:
        raise ValueError("Confirm the redaction review before exporting with it")
    if output_modes != ["individual"]:
        raise ValueError("Redacted exports contain individual camera files only")
    if set(json.loads(row["camera_ids_json"])) != set(camera_ids) or _parse(row["requested_start"]) != start or _parse(row["requested_end"]) != end:
        raise ValueError("The export must cover the same cameras and range as its redaction review")
    db.execute("UPDATE redaction_reviews SET state='used',export_id=?,updated_at=? WHERE id=?", (export_id, _now().isoformat(), review_id))


def camera_summary(regions: dict[str, Any], camera_id: str, counts: dict[str, int]) -> dict[str, int]:
    """What was blurred in one camera's clip, for the manifest."""
    tracks = [track for track in regions["tracks"] if track["camera_id"] == camera_id]
    return {"frames": counts["frames"], "blurred_boxes": counts["boxes"], "tracks_blurred": sum(1 for track in tracks if track["blur"]),
            "tracks_kept": sum(1 for track in tracks if not track["blur"]), "manual_regions": sum(1 for region in regions["manual"] if region["camera_id"] == camera_id)}


def manifest_entry(db: sqlite3.Connection, review_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The review's regions and the part of an export's manifest that records it."""
    row = db.execute("SELECT * FROM redaction_reviews WHERE id=?", (review_id,)).fetchone()
    if row is None:
        raise RuntimeError("The redaction review of this export no longer exists")
    regions = _regions(row)
    name = lambda user_id: (db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone() or [None])[0] if user_id else None  # noqa: E731
    models = json.loads(row["models_json"])
    return regions, {
        "applied": True, "review_id": review_id, "method": "pixelate", "unredacted_copy_included": False, "original_footage_unchanged": True,
        "audio": "Audio, when included, is the recording's own and is not redacted",
        "kinds": json.loads(row["kinds_json"]),
        "reviewer": {"user_id": row["confirmed_by"], "username": name(row["confirmed_by"]), "confirmed_at": row["confirmed_at"],
                     "last_edited_by": row["edited_by"], "last_edited_at": row["edited_at"]},
        "settings": {**models, "pad_fraction": PAD, "block_count_across_longer_side": BLOCKS, "hold_seconds": HOLD, "output_fps": OUTPUT_FPS},
        "kept_track_ids": sorted(track["id"] for track in regions["tracks"] if not track["blur"]),
    }


# -------------------------------------------------------------------------------------------------------------------- preview

def preview_frame(review_id: str, row: sqlite3.Row, camera_id: str, at: float) -> bytes:
    """One frame of the review's footage at ``at`` seconds into the camera's clip, outlined: red where it will be blurred, green where it will not."""
    start, end = _parse(row["requested_start"]), _parse(row["requested_end"])
    regions = _regions(row)
    held: list[Path] = []
    try:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = _sources(db, camera_id, start, end)
            if not plan["spans"]:
                raise LookupError("No footage for this camera in the review's range")
            held = _lease(plan["spans"])
        # Seek inside the one recording that holds this moment of the clip's timeline.
        from .exports import _parse_time
        span = next((item for item in plan["spans"] if item["output_start"] <= at < item["output_end"]), None)
        if span is None:
            raise LookupError("No picture at that time")
        offset = (span["source_start"] - _parse_time(span["segment"]["starts_at"])).total_seconds() + (at - span["output_start"])
        decoded = subprocess.run([_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{offset:.3f}", "-i", str(recording_file_path(span["segment"]["relative_path"])),
                                  "-map", "0:v:0", "-frames:v", "1", "-vf", _decode_filter(plan["zones"], None), "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"],
                                 capture_output=True, timeout=120, check=False)
        if decoded.returncode != 0 or len(decoded.stdout) < FRAME_BYTES:
            raise LookupError("No picture at that time")
        image = np.frombuffer(decoded.stdout[:FRAME_BYTES], dtype=np.uint8).reshape(FRAME_HEIGHT, FRAME_WIDTH, 3).copy()
        tracks = {track["id"]: track for track in regions.get("tracks", []) if track["camera_id"] == camera_id}
        for track in tracks.values():
            if (box := _interpolate(track["samples"], at, HOLD)) is not None:
                _outline(image, box, (0, 0, 255) if track["blur"] else (0, 200, 0))
        for region in regions.get("manual", []):
            if region["camera_id"] == camera_id and (box := _manual_box(region, tracks, at)) is not None:
                _outline(image, box, (0, 140, 255))
        encoded = subprocess.run([_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{FRAME_WIDTH}x{FRAME_HEIGHT}",
                                  "-i", "pipe:0", "-frames:v", "1", "-f", "mjpeg", "pipe:1"], input=image.tobytes(), capture_output=True, timeout=60, check=False)
        if encoded.returncode != 0 or not encoded.stdout:
            raise RuntimeError("The picture could not be encoded")
        return encoded.stdout
    finally:
        for path in held:
            release_playback(path)
