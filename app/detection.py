from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple

import numpy as np

from . import acceleration, ai_capacity, privacy
from .credentials import decrypt
from .processes import end_process
from .track_privacy import box_touches, box_usable
from .recording import _manifest_bytes, _PROTOCOLS, safe_stream_url
from .store import connect
from .clock import parse as clock_parse, stamp as clock_stamp

logger = logging.getLogger(__name__)

WARM_UP_FRAMES = 3
_FACTOR_TEXT = {"cpu": "CPU", "inference": "the AI model is busy", "memory": "memory", "late": "analysis is falling behind"}

MODEL_ID = "person-vehicle-bike-detection-2004"
MODEL_VERSION = "2021.4-FP32"
MODEL_RUNTIME = "OpenVINO"
MODEL_HEIGHT = 256
MODEL_WIDTH = 448
MODEL_FRAME_BYTES = MODEL_HEIGHT * MODEL_WIDTH * 3
MODEL_PATH = Path(os.environ.get("NVR_AI_MODEL_PATH", "/opt/nvr/models/person-vehicle-bike-detection-2004.xml"))
MODEL_BINARY_SHA256 = "de10beb5a811bd1b38c828b4060898c32502c4919488fd7100269cbdc7595e08"
MODEL_XML_SHA256 = "408e2a556cc935a59b765cb72045fb7c5f8071c4b0f2c5433711c2b38ff085d8"
MODEL_DIRECTORY = Path(os.environ.get("NVR_AI_MODEL_DIRECTORY", "/data/models"))


_model_versions: dict[tuple[str, int, int, int, int], str] = {}


def _model_version(path: Path) -> str:
    """The version of a model's two files. They are megabytes and this is asked for every camera every few seconds, so the answer is kept
    for as long as neither file's size or modified time changes (#348), as the plate models' is."""
    binary = path.with_suffix(".bin")
    try:
        xml_stat, binary_stat = path.stat(), binary.stat()
        key = (str(path), xml_stat.st_size, xml_stat.st_mtime_ns, binary_stat.st_size, binary_stat.st_mtime_ns)
        known = _model_versions.get(key)
        if known is not None:
            return known
        xml_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    except OSError:
        return "unavailable"
    if xml_hash == MODEL_XML_SHA256 and binary_hash == MODEL_BINARY_SHA256:
        version = MODEL_VERSION
    else:
        version = "sha256:" + hashlib.sha256((xml_hash + binary_hash).encode("ascii")).hexdigest()[:16]
    for stale in [old for old in _model_versions if old[0] == key[0]]:
        del _model_versions[stale]
    _model_versions[key] = version
    return version


def model_options() -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    candidates = [(MODEL_ID, MODEL_PATH)]
    try:
        MODEL_DIRECTORY.mkdir(parents=True, exist_ok=True)
        candidates.extend((path.stem, path) for path in sorted(MODEL_DIRECTORY.glob("*.xml"))
                          if path.resolve() != MODEL_PATH.resolve() and path.stem != MODEL_ID)
    except OSError:
        pass
    for model_id, path in candidates:
        has_pair = path.is_file() and path.with_suffix(".bin").is_file()
        options.append({"id": model_id, "version": _model_version(path) if has_pair else "unavailable", "available": has_pair,
                        "source": "bundled" if path == MODEL_PATH else "local", "compatible_contract": "448x256 BGR; boxes [x1,y1,x2,y2,confidence]; labels 0=vehicle, 1=person"})
    return options


def model_path_for(model_id: str) -> Path:
    for item in model_options():
        if item["id"] == model_id and item["available"]:
            if model_id == MODEL_ID:
                return MODEL_PATH
            path = (MODEL_DIRECTORY / f"{model_id}.xml").resolve()
            if path.parent == MODEL_DIRECTORY.resolve():
                return path
    raise ValueError("Selected AI model is unavailable")


@lru_cache(maxsize=1)
def available_openvino_devices() -> tuple[str, ...]:
    try:
        from openvino import Core
        return tuple(Core().available_devices)
    except Exception:
        return ()


def _provider_available(provider: str, devices: tuple[str, ...]) -> bool:
    return provider in devices or (provider == "GPU" and any(device.startswith("GPU.") for device in devices))


def openvino_provider_options() -> list[dict[str, Any]]:
    devices = available_openvino_devices()
    return [
        {"id": provider, "available": _provider_available(provider, devices),
         "reason": None if _provider_available(provider, devices) else f"OpenVINO {provider} is not available on this host"}
        for provider in ("CPU", "GPU")
    ]


def detector_availability(provider: str = "CPU", model_id: str = MODEL_ID) -> dict[str, Any]:
    options = {item["id"]: item for item in model_options()}
    model = options.get(model_id)
    if model is None or not model["available"]:
        return {"available": False, "reason": "Selected compatible model and its weights are unavailable"}
    devices = available_openvino_devices()
    if not _provider_available(provider, devices):
        runtime = "OpenVINO runtime is not installed" if not devices else f"OpenVINO device {provider} is unavailable on this host"
        return {"available": False, "reason": runtime, "devices": list(devices)}
    return {"available": True, "reason": None, "devices": list(devices), "model": model}


class OpenVINODetector:
    """OpenVINO adapter for models matching the NVR person/vehicle output contract."""

    def __init__(self, provider: str = "CPU", model_path: Path = MODEL_PATH, core: Any = None):
        from openvino import Core

        self.core = core or Core()
        if provider not in {"CPU", "GPU"}:
            raise ValueError("Supported OpenVINO providers are CPU and GPU")
        if not _provider_available(provider, tuple(self.core.available_devices)):
            raise RuntimeError(f"OpenVINO device {provider} is unavailable on this host")
        self.provider = provider
        self.model_path = model_path
        try:
            configured_threads = int(os.environ.get("NVR_AI_CPU_THREADS", "2"))
        except ValueError:
            configured_threads = 2
        configured_threads = max(1, min(configured_threads, 8))
        properties = {"INFERENCE_NUM_THREADS": configured_threads} if provider == "CPU" else {}
        self.compiled = self.core.compile_model(self.core.read_model(str(model_path)), provider, properties)
        # One inference request per compiled model: a second call while one runs fails with "Infer Request is busy" (#436). Cameras take
        # turns through the AI worker's inference slot, but a camera stopped mid-frame gives the slot back while its thread is still here.
        self._lock = threading.Lock()
        self.input = self.compiled.input(0)
        shape = tuple(int(dimension) for dimension in self.input.shape)
        if shape != (1, 3, MODEL_HEIGHT, MODEL_WIDTH):
            raise ValueError("Compatible models must accept a fixed 1x3x256x448 input")
        self.output_by_name = {}
        for port in self.compiled.outputs:
            names = port.get_names()
            for expected in ("boxes", "labels"):
                if expected in names:
                    self.output_by_name[expected] = port
        if "boxes" not in self.output_by_name or "labels" not in self.output_by_name:
            raise ValueError("Configured model does not expose the expected boxes and labels outputs")

    def infer(self, bgr_frame: bytes, threshold: float) -> list[dict[str, Any]]:
        if len(bgr_frame) != MODEL_FRAME_BYTES:
            raise ValueError("Frame has an unexpected size")
        image = np.frombuffer(bgr_frame, dtype=np.uint8).reshape(MODEL_HEIGHT, MODEL_WIDTH, 3)
        # The model's graph includes channel reversal and 1/255 scaling.
        tensor = image.astype(np.float32).transpose(2, 0, 1)[None, ...]
        with self._lock:
            result = self.compiled([tensor])
            # Copied while the request is still this call's: the next call reuses its output buffers
            boxes = np.array(result[self.output_by_name["boxes"]]).reshape(-1, 5)
            labels = np.array(result[self.output_by_name["labels"]]).reshape(-1)
        events = []
        for box, label in zip(boxes, labels, strict=False):
            score = float(box[4])
            class_id = int(label)
            category = "vehicle" if class_id == 0 else "person" if class_id == 1 else None
            if category is None or not np.isfinite(score) or score < threshold:
                continue
            coords = [
                max(0.0, min(1.0, float(box[0]) / MODEL_WIDTH)),
                max(0.0, min(1.0, float(box[1]) / MODEL_HEIGHT)),
                max(0.0, min(1.0, float(box[2]) / MODEL_WIDTH)),
                max(0.0, min(1.0, float(box[3]) / MODEL_HEIGHT)),
            ]
            x1, y1, x2, y2 = coords
            if x2 <= x1 or y2 <= y1:
                continue
            events.append({"label": category, "confidence": score, "box": {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}})
        return events


OpenVINOCPUDetector = OpenVINODetector


def validate_detector(provider: str, model_id: str) -> dict[str, Any]:
    available_openvino_devices.cache_clear()
    try:
        model_path = model_path_for(model_id)
        detector = OpenVINODetector(provider=provider, model_path=model_path)
        detector.infer(bytes(MODEL_FRAME_BYTES), 0.0)
    except Exception as exc:
        return {"valid": False, "provider": provider, "model_id": model_id, "reason": str(exc)[:240] or "Model validation failed"}
    return {"valid": True, "provider": provider, "model_id": model_id, "model_version": _model_version(model_path), "runtime": f"OpenVINO {provider}", "reason": None}


def point_in_zones(box: dict[str, float], zones: list[dict[str, float]]) -> bool:
    if not zones:
        return True
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    return any(z["x"] <= x <= z["x"] + z["width"] and z["y"] <= y <= z["y"] + z["height"] for z in zones)


def box_iou(left: dict[str, float], right: dict[str, float]) -> float:
    x1 = max(left["x"], right["x"])
    y1 = max(left["y"], right["y"])
    x2 = min(left["x"] + left["width"], right["x"] + right["width"])
    y2 = min(left["y"] + left["height"], right["y"] + right["height"])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = left["width"] * left["height"] + right["width"] * right["height"] - intersection
    return intersection / union if union > 0 else 0.0


DEFAULT_VEHICLE_THRESHOLD = 0.70  # Vehicles are scored lower than people and an empty doorway or hedge can reach the person threshold: ask for more unless told otherwise


def effective_vehicle_threshold(stored: float | None, person_threshold: float) -> float:
    """The confidence a vehicle needs: what was set, or the default (never less than the threshold for people)."""
    return float(stored) if stored is not None else max(float(person_threshold), DEFAULT_VEHICLE_THRESHOLD)


def keep_confident_vehicles(observations: list[dict[str, Any]], vehicle_threshold: float) -> list[dict[str, Any]]:
    return [item for item in observations if item["label"] != "vehicle" or item["confidence"] >= vehicle_threshold]


def effective_camera_config(camera_id: str) -> dict[str, Any] | None:
    with connect() as db:
        camera = db.execute("SELECT group_id FROM cameras WHERE id=? AND enabled=1", (camera_id,)).fetchone()
        if camera is None:
            return None
        personal = db.execute("SELECT * FROM ai_camera_settings WHERE camera_id=?", (camera_id,)).fetchone()
        group = db.execute("SELECT * FROM ai_group_settings WHERE group_id=?", (camera["group_id"],)).fetchone() if camera["group_id"] else None
        runtime = db.execute("SELECT provider,model_id,model_version FROM ai_runtime_settings WHERE singleton_id=1").fetchone()
    if personal and personal["config_override"]:
        result = dict(personal)
        result["config_source"] = "camera"
    elif group:
        result = dict(group)
        result["config_source"] = "group"
    else:
        result = {"enabled": 0, "analysis_fps": 0.5, "confidence_threshold": 0.55, "use_substream": 1, "zones_json": "[]", "config_source": "default"}
    result["enabled"] = bool(result["enabled"])
    result["use_substream"] = bool(result["use_substream"])
    result["vehicle_confidence_threshold"] = result.get("vehicle_confidence_threshold")  # Empty: the default
    result["effective_vehicle_confidence_threshold"] = effective_vehicle_threshold(result["vehicle_confidence_threshold"], result["confidence_threshold"])
    result["provider"] = str(runtime["provider"]) if runtime else "CPU"
    result["model_id"] = str(runtime["model_id"]) if runtime else MODEL_ID
    model = next((item for item in model_options() if item["id"] == result["model_id"]), None)
    result["model_version"] = str(runtime["model_version"]) if runtime else MODEL_VERSION
    result["model_available"] = bool(model and model["available"] and model["version"] == result["model_version"])
    return result


def record_observations(
    camera_id: str,
    observations: Iterable[dict[str, Any]],
    at: datetime | None = None,
    model_id: str = MODEL_ID,
    model_version: str | None = None,
    provider: str = "CPU",
) -> int:
    with connect() as db:
        return _record_observations(db, camera_id, observations, at, model_id, model_version, provider)


def _record_observations(
    db: sqlite3.Connection,
    camera_id: str,
    observations: Iterable[dict[str, Any]],
    at: datetime | None = None,
    model_id: str = MODEL_ID,
    model_version: str | None = None,
    provider: str = "CPU",
) -> int:
    """``record_observations`` on a connection the caller owns, so a frame can be stored in one transaction (#279)."""
    when = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = when.isoformat()
    version = model_version or MODEL_VERSION
    count = 0
    masked: list[dict[str, float]] | None = None  # The areas the NVR must hide at this moment, read once for the frame if a webhook is due
    for item in observations:
        candidates = db.execute(
            "SELECT id,box_json FROM ai_observations WHERE camera_id=? AND label=? AND model_id=? AND model_version=? AND provider=? AND last_seen_at>=? ORDER BY last_seen_at DESC LIMIT 100",
            (camera_id, item["label"], model_id, version, provider, (when - timedelta(seconds=5)).isoformat()),
        ).fetchall()
        previous = max(
            (candidate for candidate in candidates if box_iou(json.loads(candidate["box_json"]), item["box"]) >= 0.2),
            key=lambda candidate: box_iou(json.loads(candidate["box_json"]), item["box"]),
            default=None,
        )
        if previous:
            db.execute("UPDATE ai_observations SET confidence=max(confidence,?),box_json=?,last_seen_at=? WHERE id=?",
                       (item["confidence"], json.dumps(item["box"], separators=(",", ":")), stamp, previous["id"]))
        else:
            observation_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO ai_observations(id,camera_id,label,confidence,box_json,first_seen_at,last_seen_at,frame_width,frame_height,model_id,model_version,provider) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (observation_id, camera_id, item["label"], item["confidence"], json.dumps(item["box"], separators=(",", ":")), stamp, stamp, MODEL_WIDTH, MODEL_HEIGHT, model_id, version, provider),
            )
            event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "nvr-ai-observation:" + observation_id))
            from .webhooks import enqueue_event
            if masked is None:
                masked = privacy.server_zones(db, camera_id, when)
            if masked and (not box_usable(item["box"]) or box_touches(item["box"], masked)):
                # A webhook sends the data to another system: something people are not shown behind a privacy mask is not sent there either (#376).
                # Rule events and alarms are unchanged (the owner's decision on #296).
                count += 1
                continue
            enqueue_event(db, "ai.detection", {
                "event_id": event_id, "event_type": "ai.detection", "occurred_at": stamp,
                "observation_id": observation_id, "camera_id": camera_id, "label": item["label"],
                "confidence": item["confidence"], "box": item["box"], "model_id": model_id, "model_version": version, "provider": provider,
            })
            count += 1
    return count


def mark_analysis_interrupted(reason: str) -> None:
    """Record that no analysis is happening while the AI process is down."""
    with connect() as db:
        cameras = [row[0] for row in db.execute("SELECT camera_id FROM ai_camera_settings WHERE status NOT IN ('disabled','interrupted')")]
    for camera_id in cameras:
        set_camera_state(camera_id, "interrupted", reason)


def set_camera_state(camera_id: str, state: str, error: str | None = None, at: datetime | None = None) -> None:
    with connect() as db:
        _set_camera_state(db, camera_id, state, error, at)


STATE_WRITE_SECONDS = 5.0  # While a camera stays "analyzed", its row is refreshed this often; any other change is written at once
ATTEMPT_WRITE_SECONDS = 60.0  # While a camera stays failed or delayed for the same reason, the time of its last attempt is refreshed this often
RESTING_STATES = {"disabled", "skipped"}  # Nothing is being attempted: the row is written when the state or its reason changes, and not again


def _written_recently(written: str | None, moment: datetime, within: float = STATE_WRITE_SECONDS) -> bool:
    try:
        return written is not None and 0 <= (moment - clock_parse(written)).total_seconds() < within
    except (TypeError, ValueError):
        return False


def _set_camera_state(db: sqlite3.Connection, camera_id: str, state: str, error: str | None = None, at: datetime | None = None) -> None:
    """``set_camera_state`` on a connection the caller owns (#279)."""
    moment = at or datetime.now(timezone.utc)
    stamp = clock_stamp(moment)  # Canonical, like every other time written for the state and the coverage (#284)
    row = db.execute("SELECT status,last_attempt_at,last_success_at,last_error FROM ai_camera_settings WHERE camera_id=?", (camera_id,)).fetchone()
    if row is None:
        # Read first, and add the row only when it is missing: even an insert that adds nothing takes the write lock (#348).
        db.execute("INSERT OR IGNORE INTO ai_camera_settings(camera_id) VALUES(?)", (camera_id,))
        row = db.execute("SELECT status,last_attempt_at,last_success_at,last_error FROM ai_camera_settings WHERE camera_id=?", (camera_id,)).fetchone()
        if row is None:
            return
    elif row["status"] == state and (row["last_error"] or None) == (error or None):
        if state == "analyzed" and _written_recently(row["last_success_at"], moment):
            return  # Still analysing, as a moment ago: nothing to say yet. A frame at 5 fps on 20 cameras would otherwise rewrite this row 100 times a second (#279).
        if state in RESTING_STATES:
            return  # Still switched off, or still waiting for the same reason. The 5-second check used to rewrite this row for every such camera, for ever (#348).
        if state != "analyzed" and _written_recently(row["last_attempt_at"], moment, ATTEMPT_WRITE_SECONDS):
            return
    db.execute("UPDATE ai_camera_settings SET status=?,last_attempt_at=?,last_success_at=CASE WHEN ?='analyzed' THEN ? ELSE last_success_at END,last_error=?,updated_at=? WHERE camera_id=?",
               (state, stamp, state, stamp, error, stamp, camera_id))
    active = db.execute("SELECT id,state FROM ai_coverage WHERE camera_id=? AND ends_at IS NULL ORDER BY id DESC LIMIT 1", (camera_id,)).fetchone()
    if state == "disabled":
        if active:
            db.execute("UPDATE ai_coverage SET ends_at=? WHERE id=?", (stamp, active["id"]))
        return
    coverage_state = state if state in {"analyzed", "delayed", "failed", "unanalyzed"} else "unanalyzed"
    if active and active["state"] == coverage_state:
        db.execute("UPDATE ai_coverage SET detail=? WHERE id=?", (error or "", active["id"]))
    else:
        if active:
            db.execute("UPDATE ai_coverage SET ends_at=? WHERE id=?", (stamp, active["id"]))
        db.execute("INSERT INTO ai_coverage(camera_id,state,starts_at,detail) VALUES(?,?,?,?)", (camera_id, coverage_state, stamp, error or ""))
    if state in {"failed", "delayed", "analyzed"}:
        from .incidents import sync_incident
        camera = db.execute("SELECT name FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if camera:
            sync_incident(
                db,
                incident_key=f"ai_analysis:{camera_id}",
                camera_id=camera_id,
                fault_type="ai_analysis",
                summary=f"AI analysis {state}: {camera['name']}" + (f" — {error}" if error else ""),
                active=state in {"failed", "delayed"},
                details={"analysis_state": state, "reason": error},
            )


def _camera_source(camera_id: str, use_substream: bool) -> str:
    with connect() as db:
        camera = db.execute("SELECT stream_url_ciphertext,live_substream_url_ciphertext FROM cameras WHERE id=? AND enabled=1", (camera_id,)).fetchone()
    if camera is None:
        raise ValueError("Camera is disabled or unavailable")
    ciphertext = camera["live_substream_url_ciphertext"] if use_substream else None
    if use_substream and not ciphertext:
        raise ValueError("Choose the main stream or configure a validated low-resolution stream first")
    return safe_stream_url(decrypt(ciphertext or camera["stream_url_ciphertext"]))


async def _raw_frames(source: str, fps: float):
    """Decoded frames of a camera, decoded on the graphics chip when there is one. If the device gives no frames where software does, it is turned off."""
    hardware = bool(acceleration.decode_args())
    got = 0
    async for frame in _decode_frames(source, fps, hardware):
        got += 1
        yield frame
    if got == 0 and hardware:
        async for frame in _decode_frames(source, fps, False):
            if got == 0:
                acceleration.disable("hardware decoding gave no frames where software decoding did")
            got += 1
            yield frame


async def _decode_frames(source: str, fps: float, hardware: bool):
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("FFmpeg is unavailable")
    args = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", *(acceleration.decode_args() if hardware else []), "-f", "concat", "-safe", "0",
            "-protocol_whitelist", _PROTOCOLS, "-i", "pipe:0", "-an", "-vf", f"fps={fps},scale={MODEL_WIDTH}:{MODEL_HEIGHT}:flags=bilinear",
            "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    assert proc.stdin and proc.stdout
    proc.stdin.write(_manifest_bytes(source))
    await proc.stdin.drain()
    proc.stdin.close()
    try:
        while True:
            frame = await proc.stdout.readexactly(MODEL_FRAME_BYTES)
            yield frame
    except asyncio.IncompleteReadError:
        pass
    finally:
        await end_process(proc, 3)


def _with_db(write: Any, *args: Any) -> Any:
    """Run one database write on a connection of its own, for callers that run it in a thread."""
    with connect() as db:
        return write(db, *args)


def _plate_refresh(camera_id: str) -> tuple[bool, float, Any]:
    """The plate settings for a camera and the region chosen in Settings. Blocking, so the analysis loop runs it in a database thread (#279)."""
    from . import plates
    with connect() as db:
        wanted, minimum = plates.plate_settings(db, camera_id)
        return wanted, minimum, (plates.plate_region(db) if wanted else None)


def _face_refresh(camera_id: str) -> tuple[bool, float]:
    """Whether a camera looks for faces, and how alike a face must be. Blocking, so the analysis loop runs it in a database thread (#279)."""
    from . import face_scan
    with connect() as db:
        return face_scan.scanning(db, camera_id)


def _fall_refresh(camera_id: str, at: datetime) -> tuple[Any, ...]:
    """The fall settings for a camera, its picture's aspect and its privacy masks. Blocking, so the analysis loop runs it in a database thread (#279)."""
    from . import falls, privacy
    with connect() as db:
        on, sensitivity, stay_down = falls.fall_settings(db, camera_id)
        return (on, sensitivity, stay_down, falls.camera_aspect(db, camera_id) if on else 16 / 9, privacy.server_zones(db, camera_id, at) if on else [])


def _store_frame(camera_id: str, observations: list[dict[str, Any]], at: datetime, tracker: Any, frame: Any, model: dict[str, str]) -> list[str]:
    """Store one analysed frame: the camera's state, its detections and the tracks they belong to. Blocking, so callers run it in a thread."""
    from .tracks import record_detections
    # One connection and one transaction for the frame: the camera's state, its observations and its detections. Analysed coverage starts no
    # later than the detections it contains, and a frame that cannot be stored is not recorded as analysed (#279).
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _set_camera_state(db, camera_id, "analyzed", at=at)
            _record_observations(db, camera_id, observations, at, **model)
            track_ids = record_detections(db, camera_id, observations, at, tracker, frame=frame, **model)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return track_ids


_UNREAD: Any = object()  # "The caller has not read this camera's settings": None already means the camera is gone or switched off


class DetectionManager:
    def __init__(self, detector_factory: Callable[..., Any] = OpenVINODetector,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.detector_factory = detector_factory
        # When each analysed frame's results are known.
        self.clock = clock
        self.detector: Any | None = None
        self._detectors: dict[tuple[str, str, str], Any] = {}
        self._plate_readers: dict[tuple[str, str], Any] = {}
        self._pose_readers: dict[str, Any] = {}
        self._attribute_models: dict[str, Any] = {}
        self._face_readers: dict[str, Any] = {}
        self._reid_models: dict[str, Any] = {}
        self._model_loads: dict[tuple[str, Any], asyncio.Lock] = {}  # One load at a time per model, so cameras starting together share one copy
        self._model_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()  # One camera is settled at a time (see sync_camera)
        # How many times each camera has been settled from its settings as they are now (a save, a message): a reconcile pass leaves a
        # camera settled that way after its survey alone, since what the pass read about it is older.
        self._settled: dict[str, int] = {}
        # Database writes can wait seconds for a busy database; their own threads keep them from starving inference, embedding and JPEG work in the shared pool.
        self._db_threads = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ai-db")
        self._inference_slots = asyncio.Semaphore(1)
        self.capacity = ai_capacity.Capacity()
        self.limit_now: int | None = None  # How many cameras may be analysed now (from the controller, or the fixed number)
        self.waiting_text = "Waiting because the configured number of AI analysis slots is in use"
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.task_configs: dict[str, tuple[Any, ...]] = {}
        self.configuration_task: asyncio.Task[None] | None = None
        self.stopping = False

    async def start(self) -> None:
        self.stopping = False
        await self.reconcile()
        self.configuration_task = asyncio.create_task(self._configuration_loop(), name="ai-configuration-supervisor")

    def _runtime(self) -> tuple[str, int, int | None]:
        """(mode, the fixed number, the limit the status last recorded) from the settings."""
        with connect() as db:
            row = db.execute("SELECT capacity_mode,max_active_cameras FROM ai_runtime_settings WHERE singleton_id=1").fetchone()
            status = db.execute("SELECT limit_cameras FROM ai_capacity_status WHERE singleton_id=1").fetchone()
        return (str(row["capacity_mode"]), int(row["max_active_cameras"]), int(status["limit_cameras"]) if status else None) if row else ("auto", 4, None)

    def _by_importance(self, camera_ids: list[str]) -> list[str]:
        """The cameras that matter most first: those with an enabled rule, fall detection or plate reading, then the rest by name (the order they came in)."""
        with connect() as db:
            rules = db.execute("SELECT camera_ids_json,group_ids_json FROM rules WHERE enabled=1").fetchall()
            falls_on = {row[0] for row in db.execute("SELECT camera_id FROM fall_settings WHERE enabled=1")}
            plates_on = {row[0] for row in db.execute("SELECT camera_id FROM plate_settings WHERE enabled=1")}
            groups = {row["id"]: row["group_id"] for row in db.execute("SELECT id,group_id FROM cameras")}
        watched: dict[str, int] = {}
        for rule in rules:
            direct, by_group = set(json.loads(rule["camera_ids_json"])), set(json.loads(rule["group_ids_json"]))
            for camera_id in camera_ids:
                if camera_id in direct or (groups.get(camera_id) and groups[camera_id] in by_group):
                    watched[camera_id] = watched.get(camera_id, 0) + 1
        score = {camera_id: (1 if watched.get(camera_id) else 0) + (1 if camera_id in falls_on else 0) + (1 if camera_id in plates_on else 0) for camera_id in camera_ids}
        return sorted(camera_ids, key=lambda camera_id: -score[camera_id])  # Stable: ties stay in name order

    def _survey(self) -> tuple[list[str], dict[str, dict[str, Any] | None], tuple[str, int, int | None], list[str]]:
        """Everything a pass reads before it decides: the cameras, each one's settings, the limit, and who wants analysis, most important
        first. Blocking; the pass runs it in a thread so a busy database cannot stop the AI worker's loop and its heartbeat (#348)."""
        with connect() as db:
            cameras = [row["id"] for row in db.execute("SELECT id FROM cameras WHERE enabled=1 ORDER BY name COLLATE NOCASE")]
        configs = {camera_id: effective_camera_config(camera_id) for camera_id in cameras}
        wanting = self._by_importance([camera_id for camera_id in cameras if configs[camera_id] and configs[camera_id]["enabled"]])
        return cameras, configs, self._runtime(), wanting

    async def reconcile(self) -> None:
        settled_before = dict(self._settled)
        cameras, configs, (mode, fixed, last_limit), wanting = await asyncio.to_thread(self._survey)
        analysed = sum(1 for camera_id in wanting if camera_id in self.tasks and not self.tasks[camera_id].done())
        if mode == "fixed":
            limit = fixed
            self.waiting_text = f"Waiting because the fixed limit of {fixed} analyzed camera{'s' if fixed != 1 else ''} is in use"
        else:
            if self.capacity.controller is None or self.capacity.due():
                decision = await asyncio.to_thread(self.capacity.evaluate, analysed, len(wanting), last_limit)
                if decision.action in {"shed", "grow"}:
                    logger.info("AI capacity: %s to %d cameras (%s)", decision.action, decision.limit, decision.detail)
            controller = self.capacity.controller
            limit = controller.limit if controller else ai_capacity.START_CAMERAS
            factor = self.capacity.controller.last.factor if self.capacity.controller and self.capacity.controller.last else None
            self.waiting_text = "Waiting: this machine is at its limit" + (f" ({_FACTOR_TEXT.get(factor, factor)})" if factor else "")
        self.limit_now = limit
        eligible = wanting[:limit]
        for camera_id in cameras:
            if self._settled.get(camera_id, 0) != settled_before.get(camera_id, 0):
                # Its settings were saved and applied after this pass read them: what the pass read would undo them until the next pass.
                continue
            await self.sync_camera(camera_id, capacity_allowed=camera_id in eligible, config=configs[camera_id])
        await asyncio.to_thread(self._write_status, mode, limit, len(wanting))

    def _write_status(self, mode: str, limit: int, wanting: int) -> None:
        analysed = sum(1 for task in self.tasks.values() if not task.done())
        status = self.capacity.status(mode, limit, analysed, wanting)
        with connect() as db:
            db.execute(
                "INSERT INTO ai_capacity_status(singleton_id,mode,limit_cameras,analysed,wanting,factor,action,cpu,cores,inference_busy,memory_used_bytes,memory_total_bytes,late_share,detail,updated_at) "
                "VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(singleton_id) DO UPDATE SET mode=excluded.mode,limit_cameras=excluded.limit_cameras,analysed=excluded.analysed,"
                "wanting=excluded.wanting,factor=excluded.factor,action=excluded.action,cpu=excluded.cpu,cores=excluded.cores,inference_busy=excluded.inference_busy,"
                "memory_used_bytes=excluded.memory_used_bytes,memory_total_bytes=excluded.memory_total_bytes,late_share=excluded.late_share,detail=excluded.detail,updated_at=excluded.updated_at",
                (status["mode"], status["limit_cameras"], status["analysed"], status["wanting"], status["factor"], status["action"], status["cpu"], status["cores"], status["inference_busy"],
                 status["memory_used_bytes"], status["memory_total_bytes"], status["late_share"], status["detail"], datetime.now(timezone.utc).isoformat(timespec="seconds")))

    async def _configuration_loop(self) -> None:
        while not self.stopping:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(5)

    async def stop(self) -> None:
        self.stopping = True
        if self.configuration_task and not self.configuration_task.done():
            self.configuration_task.cancel()
            await asyncio.gather(self.configuration_task, return_exceptions=True)
        self.configuration_task = None
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.task_configs.clear()
        self._detectors.clear()

    def _current_limit(self) -> int:
        """The limit before the first reconcile: the fixed number, or where the controller starts."""
        mode, fixed, last_limit = self._runtime()
        return fixed if mode == "fixed" else (ai_capacity.Controller.starting(ai_capacity.hardware_ceiling(self.capacity.sampler.cores(), self.capacity.sampler.memory_total()), last_limit).limit)

    async def sync_camera(self, camera_id: str, force_restart: bool = False, capacity_allowed: bool | None = None, config: Any = _UNREAD) -> None:
        """Start, restart or stop one camera's analysis to match its settings. ``config`` is the camera's settings when the caller has
        just read them. Every database call is made in a thread, and one camera is settled at a time, so a message arriving in the middle
        of a pass cannot start a second analysis of a camera."""
        if config is _UNREAD:  # Settled from its settings as they are now, read below
            self._settled[camera_id] = self._settled.get(camera_id, 0) + 1
        async with self._sync_lock:
            await self._sync_camera(camera_id, force_restart, capacity_allowed, config)

    async def _sync_camera(self, camera_id: str, force_restart: bool, capacity_allowed: bool | None, config: Any) -> None:
        if self.stopping:
            return
        if config is _UNREAD:
            config = await asyncio.to_thread(effective_camera_config, camera_id)
        set_camera_state = self._state
        task = self.tasks.get(camera_id)
        signature = None if config is None else (
            bool(config["enabled"]), float(config["analysis_fps"]), float(config["confidence_threshold"]), float(config["effective_vehicle_confidence_threshold"]),
            bool(config["use_substream"]), config["zones_json"], config["config_source"],
            config["provider"], config["model_id"], config["model_version"],
        )
        if task and not task.done() and self.task_configs.get(camera_id) != signature:
            force_restart = True
        if force_restart and task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.tasks.pop(camera_id, None)
            self.task_configs.pop(camera_id, None)
            task = None
        if not config or not config["enabled"]:
            if task and not task.done():
                task.cancel()
            self.tasks.pop(camera_id, None)
            self.task_configs.pop(camera_id, None)
            await set_camera_state(camera_id, "disabled")
            return
        if not config.get("model_available", False) and self.detector is None:
            await set_camera_state(camera_id, "failed", "Selected model changed or is unvalidated; validate and save the AI runtime settings")
            return
        if capacity_allowed is False:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.tasks.pop(camera_id, None)
            self.task_configs.pop(camera_id, None)
            await set_camera_state(camera_id, "skipped", self.waiting_text)
            return
        active_count = sum(1 for active in self.tasks.values() if not active.done())
        max_active = self.limit_now if self.limit_now is not None else await asyncio.to_thread(self._current_limit)
        if task is None and active_count >= max_active:
            await set_camera_state(camera_id, "skipped", self.waiting_text)
            self.task_configs.pop(camera_id, None)
            return
        if task is None or task.done() or force_restart:
            detector = self.detector
            detector_key = (str(config["provider"]), str(config["model_id"]), str(config["model_version"]))
            if detector is None:
                async with self._model_lock:
                    detector = self._detectors.get(detector_key)
                    if detector is None:
                        try:
                            detector = await asyncio.to_thread(
                                self.detector_factory, str(config["provider"]), model_path_for(str(config["model_id"]))
                            )
                            self._detectors[detector_key] = detector
                        except Exception:
                            await set_camera_state(camera_id, "failed", "Selected OpenVINO provider/model could not be loaded")
                            return
            if detector is None:
                return
            await set_camera_state(camera_id, "connecting")
            self.tasks[camera_id] = asyncio.create_task(self._run(camera_id, config, detector), name=f"ai-detection-{camera_id}")
            self.task_configs[camera_id] = signature

    async def _state(self, camera_id: str, state: str, error: str | None = None) -> None:
        await self._db(set_camera_state, camera_id, state, error)

    async def _shared_model(self, name: str, cache: dict[Any, Any], key: Any, load: Any, *args: Any, remember_missing: bool) -> Any:
        """A model loaded once for every camera that asks, however many ask at once (#217). Cameras start together when the worker does: each
        used to see no model yet and load its own copy, and keep it for as long as it ran. A missing model is remembered only when
        ``remember_missing``: the pose, plate and face models can be installed while the worker runs, and are looked for again."""
        if key in cache and (cache[key] is not None or remember_missing):
            return cache[key]
        async with self._model_loads.setdefault((name, key), asyncio.Lock()):
            if key in cache and (cache[key] is not None or remember_missing):
                return cache[key]  # Loaded by the camera that asked first
            model = await asyncio.to_thread(load, *args)
            if model is not None or remember_missing:
                cache[key] = model
            return model

    async def _attribute_model(self, provider: str) -> Any:
        """The vehicle attribute model, loaded once for every camera that uses this provider (None when it is not installed)."""
        from . import attributes
        return await self._shared_model("attributes", self._attribute_models, provider, attributes.load_attribute_model,
                                        attributes.ATTRIBUTE_MODEL_PATH, provider, remember_missing=True)

    async def _db(self, write: Any, *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._db_threads, write, *args)

    async def _pose_reader(self, provider: str) -> Any:
        """The pose model, loaded once for every camera with fall detection that uses this provider (None when it is not installed or will not load, and then not remembered)."""
        from . import pose_models
        return await self._shared_model("pose", self._pose_readers, provider, pose_models.load_reader, provider, remember_missing=False)

    async def _plate_reader(self, provider: str, region: str) -> Any:
        """The plate detector and recogniser for a region, loaded once for every plate camera that uses this provider and region (None when they are not installed)."""
        from . import plates
        # A failed load is not remembered, so installing or re-choosing the models works without a restart.
        return await self._shared_model("plates", self._plate_readers, (provider, region), plates.load_reader, provider, region, remember_missing=False)

    async def _face_reader(self, provider: str) -> Any:
        """The face detector and recogniser, loaded once for every camera that looks for faces on this provider (None when they are not installed)."""
        from . import face_models
        # A failed load is not remembered, so installing the models works without a restart.
        return await self._shared_model("faces", self._face_readers, provider, face_models.load_reader, provider, remember_missing=False)

    async def _reid_model(self, provider: str) -> Any:
        """The person re-identification model, loaded once for every camera that uses this provider (None when it is not installed)."""
        from . import similar
        return await self._shared_model("reid", self._reid_models, provider, similar.load_reid_model, similar.REID_MODEL_PATH, provider, remember_missing=True)

    async def _clip_model(self) -> Any:
        """The administrator's image-text model, loaded once for every camera (None when none is installed)."""
        from . import clip
        if not hasattr(self, "_clip_loaded"):
            self._clip_loaded = await asyncio.to_thread(clip.shared_model)  # Loaded once in the process, however many cameras ask at once
        return self._clip_loaded

    async def _run(self, camera_id: str, config: dict[str, Any], detector: Any) -> None:
        """Analyse one camera until the worker stops. A stream that fails or ends marks the camera failed, and is opened again 5 s later."""
        provider = str(config["provider"])
        models = _FrameModels(await self._attribute_model(provider), await self._reid_model(provider), await self._clip_model())
        background: set[asyncio.Task[Any]] = set()  # The plate and face reads under way, kept until they finish
        while not self.stopping:
            try:
                seen_frame = await self._analyse_stream(camera_id, config, detector, models, background)
                if not self.stopping:
                    raise RuntimeError("Camera stream ended" + (" after analysis frames" if seen_frame else " before an analysis frame was received"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = str(exc)[:240] or "Inference stream failed"
                await self._db(set_camera_state, camera_id, "failed", reason)
                await asyncio.sleep(5)

    async def _analyse_stream(self, camera_id: str, config: dict[str, Any], detector: Any, models: _FrameModels,
                              background: set[asyncio.Task[Any]]) -> bool:
        """Analyse frames from one opening of the camera's stream until it ends or the worker stops. Says whether any frame arrived."""
        from . import attributes
        from .tracks import Tracker

        source = _camera_source(camera_id, bool(config["use_substream"]))
        zones = json.loads(config["zones_json"])
        tracker = Tracker()
        provider = str(config["provider"])
        watches = (_PlateWatch(self, camera_id, provider, background), _FaceWatch(self, camera_id, provider, background),
                   _FallWatch(self, camera_id, provider))
        keyframed: set[str] = set()
        frames_seen = 0
        async for frame in _raw_frames(source, float(config["analysis_fps"])):
            if self.stopping:
                return frames_seen > 0
            frames_seen += 1
            observations = await self._detect(camera_id, config, detector, frame, frames_seen)
            observations = [item for item in observations if point_in_zones(item["box"], zones)]
            await asyncio.to_thread(attributes.describe, frame, observations, models.attributes)
            at = self.clock()
            model = {"model_id": str(config["model_id"]), "model_version": str(config["model_version"]), "provider": provider}
            # The database writes wait up to 10 s each for a busy database, so they run off the event loop: a loop blocked for 30 s
            # stops the heartbeat and the supervisor kills the whole worker (exit code -9), taking every camera's analysis with it.
            track_ids = await self._db(_store_frame, camera_id, observations, at, tracker, frame, model)
            for watch in watches:
                await watch.after_frame(frame, at, track_ids, observations, tracker)
            await self._keep_first_sightings(frame, at, track_ids, observations, keyframed, models)
        return frames_seen > 0

    async def _detect(self, camera_id: str, config: dict[str, Any], detector: Any, frame: Any, frames_seen: int) -> list[dict[str, Any]]:
        """The confident people and vehicles in one frame, read when the camera's turn at the shared inference slots comes."""
        wait_started = asyncio.get_running_loop().time()
        async with self._inference_slots:
            queue_wait = asyncio.get_running_loop().time() - wait_started
            if queue_wait > max(1.0, 2 / float(config["analysis_fps"])):
                if frames_seen > WARM_UP_FRAMES:  # The first frames wait behind other cameras' model start-up: not a sign of too little capacity
                    self.capacity.note_late(camera_id)
                await self._db(set_camera_state, camera_id, "delayed", "Inference was delayed while CPU capacity was occupied")
            infer_started = time.monotonic()
            person_threshold, vehicle_threshold = float(config["confidence_threshold"]), float(config["effective_vehicle_confidence_threshold"])
            observations = await asyncio.to_thread(detector.infer, frame, min(person_threshold, vehicle_threshold))
            observations = [item for item in observations if item["label"] == "vehicle" or item["confidence"] >= person_threshold]
            self.capacity.note_inference(time.monotonic() - infer_started)
        return keep_confident_vehicles(observations, vehicle_threshold)

    async def _keep_first_sightings(self, frame: Any, at: datetime, track_ids: list[str], observations: list[dict[str, Any]],
                                    keyframed: set[str], models: _FrameModels) -> None:
        """Each object's first analysed frame, kept as its keyframe, with what it looks like for finding it again and for searching in words."""
        from . import clip, similar
        from .tracks import encode_jpeg, store_keyframe

        first_seen = {track_id: item for track_id, item in reversed(list(zip(track_ids, observations)))}
        for track_id in dict.fromkeys(track_ids):
            if track_id in keyframed:
                continue
            keyframed.add(track_id)
            jpeg = await asyncio.to_thread(encode_jpeg, frame)
            if jpeg:
                await self._db(_with_db, store_keyframe, track_id, at, jpeg)
            item = first_seen.get(track_id)
            # What the object looks like, for finding it again on other cameras. A failure here never stops detection.
            try:
                described = await asyncio.to_thread(similar.embed, frame, str(item["label"]), item["box"], models.appearance) if item else None
                if described:
                    await self._db(_with_db, similar.store_embedding, track_id, described[0], described[1])
            except Exception:  # noqa: BLE001
                pass
            # What it looks like to the image-text model, for natural-language search. Also never fatal.
            clip_model = models.image_text
            try:
                vector = await asyncio.to_thread(clip_model.embed_image, frame, item["box"]) if clip_model and item else None
                if vector is not None:
                    await self._db(_with_db, clip.store_embedding, track_id, clip_model.model_id, vector)
            except Exception:  # noqa: BLE001
                pass


class _FrameModels(NamedTuple):
    """The models every analysed frame of a camera is described with, loaded once for the camera's analysis."""
    attributes: Any
    appearance: Any
    image_text: Any


def _keep_until_done(tasks: set[asyncio.Task[Any]], work: Any, name: str) -> None:
    task = asyncio.create_task(work, name=name)
    tasks.add(task)
    task.add_done_callback(tasks.discard)


class _PlateWatch:
    """A camera designated for plates gives each vehicle a few attempts at its plate; reading never holds up analysis.

    The settings are read again every ``plates.SETTINGS_REFRESH`` seconds, so a change (including the region chosen in Settings) takes
    effect without a restart."""

    def __init__(self, manager: DetectionManager, camera_id: str, provider: str, background: set[asyncio.Task[Any]]) -> None:
        self.manager, self.camera_id, self.provider, self.background = manager, camera_id, provider, background
        self.scanner: Any = None
        self.reader: Any = None
        self.checked = float("-inf")

    async def after_frame(self, frame: Any, at: datetime, track_ids: list[str], observations: list[dict[str, Any]], tracker: Any) -> None:
        from . import plates
        manager, camera_id = self.manager, self.camera_id
        if time.monotonic() - self.checked >= plates.SETTINGS_REFRESH:  # Not the manager's clock: the analysis clock stamps the frames and must be read once per frame
            self.checked = time.monotonic()
            wanted, minimum, region = await manager._db(_plate_refresh, camera_id)
            plate_reader = await manager._plate_reader(self.provider, region) if wanted and region else None
            if not wanted or plate_reader is None:
                self.scanner = None
            elif self.scanner is None or self.reader is not plate_reader:
                self.reader = plate_reader
                self.scanner = plates.PlateScanner(
                    plate_reader, lambda: plates.ffmpeg_snapshot(_camera_source(camera_id, False)),
                    lambda track, plate, reader=plate_reader: plates.keep_plate(camera_id, track, plate, manager.clock(), reader.reader_id), min_confidence=minimum)
            else:
                self.scanner.min_confidence = minimum
        scanner = self.scanner
        if scanner is not None:
            for track_id, item in zip(track_ids, observations):
                scanner.seen(track_id, str(item["label"]), item["box"])
            scanner.forget({track.id for track in tracker.active})
            if scanner.due():
                _keep_until_done(self.background, scanner.scan(), f"plates-{camera_id}")


class _FaceWatch:
    """A camera switched on for the face watch gives each person a few attempts at a face that matches someone on the list; reading never
    holds up analysis. The settings are read again every ``face_scan.SETTINGS_REFRESH`` seconds."""

    def __init__(self, manager: DetectionManager, camera_id: str, provider: str, background: set[asyncio.Task[Any]]) -> None:
        self.manager, self.camera_id, self.provider, self.background = manager, camera_id, provider, background
        self.scanner: Any = None
        self.reader: Any = None
        self.checked = float("-inf")

    async def after_frame(self, frame: Any, at: datetime, track_ids: list[str], observations: list[dict[str, Any]], tracker: Any) -> None:
        from . import face_scan, plates
        camera_id = self.camera_id
        if time.monotonic() - self.checked >= face_scan.SETTINGS_REFRESH:
            self.checked = time.monotonic()
            face_on, face_threshold = await self.manager._db(_face_refresh, camera_id)
            face_reader = await self.manager._face_reader(self.provider) if face_on else None
            if face_reader is None:
                self.scanner = None
            elif self.scanner is None or self.reader is not face_reader:
                self.reader = face_reader
                self.scanner = face_scan.FaceScanner(
                    face_reader, lambda: plates.ffmpeg_snapshot(_camera_source(camera_id, False)), face_scan.watch_list,
                    lambda track, entry, similarity, face, picture, taken: face_scan.keep_sighting(camera_id, track, entry, similarity, face, picture, taken),
                    threshold=face_threshold)
            else:
                self.scanner.threshold = face_threshold
        scanner = self.scanner
        if scanner is not None:
            for track_id, item in zip(track_ids, observations):
                scanner.seen(track_id, str(item["label"]), item["box"])
            scanner.forget({track.id for track in tracker.active})
            if scanner.due():
                _keep_until_done(self.background, scanner.scan(), f"faces-{camera_id}")


class _FallWatch:
    """A camera with fall detection on has each person's body points read and watched; a fall raises the alarm. This never holds up
    analysis for long and never stops it. The settings are read again every ``falls.SETTINGS_REFRESH`` seconds."""

    def __init__(self, manager: DetectionManager, camera_id: str, provider: str) -> None:
        self.manager, self.camera_id, self.provider = manager, camera_id, provider
        self.detector: Any = None
        self.reader: Any = None
        self.zones: Any = None
        self.checked = float("-inf")

    async def after_frame(self, frame: Any, at: datetime, track_ids: list[str], observations: list[dict[str, Any]], tracker: Any) -> None:
        from . import falls
        if time.monotonic() - self.checked >= falls.SETTINGS_REFRESH:
            self.checked = time.monotonic()
            fall_on, sensitivity, stay_down, fall_aspect, self.zones = await self.manager._db(_fall_refresh, self.camera_id, at)
            self.reader = await self.manager._pose_reader(self.provider) if fall_on else None
            if self.reader is None:
                self.detector = None
            elif self.detector is None:
                self.detector = falls.FallDetector(sensitivity=sensitivity, stay_down_seconds=stay_down, aspect=fall_aspect)
            else:
                self.detector.sensitivity, self.detector.stay_down_seconds, self.detector.aspect = sensitivity, stay_down, fall_aspect
        if self.detector is not None and self.reader is not None:
            try:
                await self._watch(frame, at, track_ids, observations, tracker)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - fall detection must never stop analysis
                logger.exception("Fall detection failed on a frame")

    async def _watch(self, frame: Any, at: datetime, track_ids: list[str], observations: list[dict[str, Any]], tracker: Any) -> None:
        from . import falls, plates, pose_models
        from .tracks import store_poses
        detector, reader, camera_id = self.detector, self.reader, self.camera_id
        moment = at.timestamp()
        picture = np.frombuffer(frame, dtype=np.uint8).reshape(MODEL_HEIGHT, MODEL_WIDTH, 3)  # The analysed frame: small, so far-off people are rough
        # Never anyone inside a privacy mask: a masked area yields no picture to read, as for plates.
        people = [(track_id, item) for track_id, item in zip(track_ids, observations)
                  if item["label"] == "person" and not plates.overlaps_privacy_zone(item["box"], self.zones)][:falls.MAX_PEOPLE_PER_FRAME]
        boxes = {track_id: item["box"] for track_id, item in people}
        raised = []
        drawn: dict[str, dict[str, tuple[float, float, float]]] = {}
        for track_id, item in people:
            full = await asyncio.to_thread(reader.pose_full, picture, item["box"])
            pose = None if full is None else {name: point for name, point in full.items() if name in pose_models.POINTS}  # The decision uses these; the arms are only for drawing
            if full is not None:
                drawn[track_id] = full
            found = detector.update(track_id, moment, pose, centre=(item["box"]["x"] + item["box"]["width"] / 2, item["box"]["y"] + item["box"]["height"] / 2))
            if found:
                raised.append(found)
        if drawn:
            await self.manager._db(_with_db, store_poses, camera_id, at, drawn)  # So Playback can draw the skeleton; a failure here never stops detection
        raised += detector.tick(moment, set(boxes))
        detector.forget({track.id for track in tracker.active}, moment)
        for found in raised:
            await self.manager._db(falls.store_fall, camera_id, found.track_id, datetime.fromtimestamp(found.at, timezone.utc), found.down_seconds,
                                   boxes.get(found.track_id) or falls.FULL_FRAME_BOX)
