from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from .credentials import decrypt
from .recording import _manifest_bytes, _PROTOCOLS, safe_stream_url
from .store import connect

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


def _model_version(path: Path) -> str:
    binary = path.with_suffix(".bin")
    try:
        xml_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    except OSError:
        return "unavailable"
    if xml_hash == MODEL_XML_SHA256 and binary_hash == MODEL_BINARY_SHA256:
        return MODEL_VERSION
    digest = hashlib.sha256((xml_hash + binary_hash).encode("ascii")).hexdigest()
    return "sha256:" + digest[:16]


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
        result = self.compiled([tensor])
        boxes = np.asarray(result[self.output_by_name["boxes"]]).reshape(-1, 5)
        labels = np.asarray(result[self.output_by_name["labels"]]).reshape(-1)
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
    when = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = when.isoformat()
    version = model_version or MODEL_VERSION
    count = 0
    with connect() as db:
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
    stamp = (at or datetime.now(timezone.utc)).isoformat()
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO ai_camera_settings(camera_id) VALUES(?)", (camera_id,))
        row = db.execute("SELECT status FROM ai_camera_settings WHERE camera_id=?", (camera_id,)).fetchone()
        if row is None:
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
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("FFmpeg is unavailable")
    args = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
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
        if proc.returncode is None:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


class DetectionManager:
    def __init__(self, detector_factory: Callable[..., Any] = OpenVINODetector,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.detector_factory = detector_factory
        # When each analysed frame's results are known.
        self.clock = clock
        self.detector: Any | None = None
        self._detectors: dict[tuple[str, str, str], Any] = {}
        self._model_lock = asyncio.Lock()
        self._inference_slots = asyncio.Semaphore(1)
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.task_configs: dict[str, tuple[Any, ...]] = {}
        self.configuration_task: asyncio.Task[None] | None = None
        self.stopping = False

    async def start(self) -> None:
        self.stopping = False
        await self.reconcile()
        self.configuration_task = asyncio.create_task(self._configuration_loop(), name="ai-configuration-supervisor")

    async def reconcile(self) -> None:
        with connect() as db:
            cameras = db.execute("SELECT id FROM cameras WHERE enabled=1 ORDER BY name COLLATE NOCASE").fetchall()
            runtime = db.execute("SELECT max_active_cameras FROM ai_runtime_settings WHERE singleton_id=1").fetchone()
        max_active = int(runtime["max_active_cameras"]) if runtime else 4
        eligible: list[str] = []
        for row in cameras:
            config = effective_camera_config(row["id"])
            if config and config["enabled"] and len(eligible) < max_active:
                eligible.append(row["id"])
        for row in cameras:
            await self.sync_camera(row["id"], capacity_allowed=row["id"] in eligible)

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

    async def sync_camera(self, camera_id: str, force_restart: bool = False, capacity_allowed: bool | None = None) -> None:
        if self.stopping:
            return
        config = effective_camera_config(camera_id)
        task = self.tasks.get(camera_id)
        signature = None if config is None else (
            bool(config["enabled"]), float(config["analysis_fps"]), float(config["confidence_threshold"]),
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
            set_camera_state(camera_id, "disabled")
            return
        if not config.get("model_available", False) and self.detector is None:
            set_camera_state(camera_id, "failed", "Selected model changed or is unvalidated; validate and save the AI runtime settings")
            return
        if capacity_allowed is False:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.tasks.pop(camera_id, None)
            self.task_configs.pop(camera_id, None)
            set_camera_state(camera_id, "skipped", "Waiting because the configured number of AI analysis slots is in use")
            return
        active_count = sum(1 for active in self.tasks.values() if not active.done())
        with connect() as db:
            runtime = db.execute("SELECT max_active_cameras FROM ai_runtime_settings WHERE singleton_id=1").fetchone()
        max_active = int(runtime["max_active_cameras"]) if runtime else 4
        if task is None and active_count >= max_active:
            set_camera_state(camera_id, "skipped", "Waiting because the configured number of AI analysis slots is in use")
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
                            set_camera_state(camera_id, "failed", "Selected OpenVINO provider/model could not be loaded")
                            return
            if detector is None:
                return
            set_camera_state(camera_id, "connecting")
            self.tasks[camera_id] = asyncio.create_task(self._run(camera_id, config, detector), name=f"ai-detection-{camera_id}")
            self.task_configs[camera_id] = signature

    async def _attribute_model(self, provider: str) -> Any:
        """The vehicle attribute model, loaded once for every camera that uses this provider (None when it is not installed)."""
        from . import attributes
        if not hasattr(self, "_attribute_models"):
            self._attribute_models: dict[str, Any] = {}
        if provider not in self._attribute_models:
            self._attribute_models[provider] = await asyncio.to_thread(attributes.load_attribute_model, attributes.ATTRIBUTE_MODEL_PATH, provider)
        return self._attribute_models[provider]

    async def _reid_model(self, provider: str) -> Any:
        """The person re-identification model, loaded once for every camera that uses this provider (None when it is not installed)."""
        from . import similar
        if not hasattr(self, "_reid_models"):
            self._reid_models: dict[str, Any] = {}
        if provider not in self._reid_models:
            self._reid_models[provider] = await asyncio.to_thread(similar.load_reid_model, similar.REID_MODEL_PATH, provider)
        return self._reid_models[provider]

    async def _run(self, camera_id: str, config: dict[str, Any], detector: Any) -> None:
        from . import attributes
        from . import similar
        from .tracks import Tracker, encode_jpeg, record_detections, store_keyframe

        attribute_model = await self._attribute_model(str(config["provider"]))
        reid_model = await self._reid_model(str(config["provider"]))
        while not self.stopping:
            try:
                source = _camera_source(camera_id, bool(config["use_substream"]))
                zones = json.loads(config["zones_json"])
                tracker = Tracker()
                keyframed: set[str] = set()
                seen_frame = False
                async for frame in _raw_frames(source, float(config["analysis_fps"])):
                    if self.stopping:
                        return
                    seen_frame = True
                    wait_started = asyncio.get_running_loop().time()
                    async with self._inference_slots:
                        queue_wait = asyncio.get_running_loop().time() - wait_started
                        if queue_wait > max(1.0, 2 / float(config["analysis_fps"])):
                            set_camera_state(camera_id, "delayed", "Inference was delayed while CPU capacity was occupied")
                        observations = await asyncio.to_thread(detector.infer, frame, float(config["confidence_threshold"]))
                    observations = [item for item in observations if point_in_zones(item["box"], zones)]
                    await asyncio.to_thread(attributes.describe, frame, observations, attribute_model)
                    at = self.clock()
                    # Analysed coverage starts no later than the detections it contains.
                    set_camera_state(camera_id, "analyzed", at=at)
                    model = {"model_id": str(config["model_id"]), "model_version": str(config["model_version"]), "provider": str(config["provider"])}
                    record_observations(camera_id, observations, at, **model)
                    with connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        try:
                            track_ids = record_detections(db, camera_id, observations, at, tracker, frame=frame, **model)
                        except BaseException:
                            db.execute("ROLLBACK")
                            raise
                        db.execute("COMMIT")
                    # Each object's first analysed frame, kept as its keyframe.
                    first_seen = {track_id: item for track_id, item in reversed(list(zip(track_ids, observations)))}
                    for track_id in dict.fromkeys(track_ids):
                        if track_id in keyframed:
                            continue
                        keyframed.add(track_id)
                        jpeg = await asyncio.to_thread(encode_jpeg, frame)
                        if jpeg:
                            with connect() as db:
                                store_keyframe(db, track_id, at, jpeg)
                        item = first_seen.get(track_id)
                        # What the object looks like, for finding it again on other cameras. A failure here never stops detection.
                        try:
                            described = await asyncio.to_thread(similar.embed, frame, str(item["label"]), item["box"], reid_model) if item else None
                            if described:
                                with connect() as db:
                                    similar.store_embedding(db, track_id, described[0], described[1])
                        except Exception:  # noqa: BLE001
                            pass
                if not self.stopping:
                    raise RuntimeError("Camera stream ended" + (" after analysis frames" if seen_frame else " before an analysis frame was received"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = str(exc)[:240] or "Inference stream failed"
                set_camera_state(camera_id, "failed", reason)
                await asyncio.sleep(5)

