from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from .credentials import decrypt
from .recording import _manifest_bytes, _PROTOCOLS, safe_stream_url
from .store import connect

MODEL_ID = "person-vehicle-bike-detection-2004"
MODEL_VERSION = "2021.4-FP32"
MODEL_RUNTIME = "OpenVINO CPU"
MODEL_HEIGHT = 256
MODEL_WIDTH = 448
MODEL_FRAME_BYTES = MODEL_HEIGHT * MODEL_WIDTH * 3
MODEL_PATH = Path(os.environ.get("NVR_AI_MODEL_PATH", "/opt/nvr/models/person-vehicle-bike-detection-2004.xml"))
MODEL_BINARY_SHA256 = "de10beb5a811bd1b38c828b4060898c32502c4919488fd7100269cbdc7595e08"
MODEL_XML_SHA256 = "408e2a556cc935a59b765cb72045fb7c5f8071c4b0f2c5433711c2b38ff085d8"


def detector_availability() -> dict[str, Any]:
    try:
        import openvino  # noqa: F401
    except ImportError:
        return {"available": False, "reason": "CPU inference runtime is not installed"}
    binary = MODEL_PATH.with_suffix(".bin")
    if not MODEL_PATH.is_file() or not binary.is_file():
        return {"available": False, "reason": "Bundled detection model is unavailable"}
    return {"available": True, "reason": None}


class OpenVINOCPUDetector:
    """CPU-only adapter for the pinned Open Model Zoo detector."""

    def __init__(self, model_path: Path = MODEL_PATH, core: Any = None):
        from openvino import Core

        self.core = core or Core()
        try:
            configured_threads = int(os.environ.get("NVR_AI_CPU_THREADS", "2"))
        except ValueError:
            configured_threads = 2
        configured_threads = max(1, min(configured_threads, 8))
        self.compiled = self.core.compile_model(self.core.read_model(str(model_path)), "CPU", {"INFERENCE_NUM_THREADS": configured_threads})
        self.input = self.compiled.input(0)
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


def point_in_zones(box: dict[str, float], zones: list[dict[str, float]]) -> bool:
    if not zones:
        return True
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    return any(z["x"] <= x <= z["x"] + z["width"] and z["y"] <= y <= z["y"] + z["height"] for z in zones)


def record_observations(camera_id: str, observations: Iterable[dict[str, Any]], at: datetime | None = None) -> int:
    when = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = when.isoformat()
    count = 0
    with connect() as db:
        for item in observations:
            previous = db.execute(
                "SELECT id,box_json FROM ai_observations WHERE camera_id=? AND label=? AND last_seen_at>=? ORDER BY last_seen_at DESC LIMIT 1",
                (camera_id, item["label"], (when - timedelta(seconds=5)).isoformat()),
            ).fetchone()
            if previous:
                db.execute("UPDATE ai_observations SET confidence=max(confidence,?),box_json=?,last_seen_at=? WHERE id=?",
                           (item["confidence"], json.dumps(item["box"], separators=(",", ":")), stamp, previous["id"]))
            else:
                db.execute(
                    "INSERT INTO ai_observations(id,camera_id,label,confidence,box_json,first_seen_at,last_seen_at,frame_width,frame_height,model_id,model_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), camera_id, item["label"], item["confidence"], json.dumps(item["box"], separators=(",", ":")), stamp, stamp, MODEL_WIDTH, MODEL_HEIGHT, MODEL_ID, MODEL_VERSION),
                )
                count += 1
    return count


def set_camera_state(camera_id: str, state: str, error: str | None = None, at: datetime | None = None) -> None:
    stamp = (at or datetime.now(timezone.utc)).isoformat()
    with connect() as db:
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
    def __init__(self, detector_factory: Callable[[], Any] = OpenVINOCPUDetector):
        self.detector_factory = detector_factory
        self.detector: Any | None = None
        self._model_lock = asyncio.Lock()
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.stopping = False

    async def start(self) -> None:
        self.stopping = False
        with connect() as db:
            enabled = db.execute("SELECT camera_id FROM ai_camera_settings WHERE enabled=1 ORDER BY updated_at").fetchall()
        if enabled:
            for row in enabled:
                await self.sync_camera(row["camera_id"])

    async def stop(self) -> None:
        self.stopping = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def sync_camera(self, camera_id: str, force_restart: bool = False) -> None:
        if self.stopping:
            return
        with connect() as db:
            config = db.execute("SELECT enabled FROM ai_camera_settings WHERE camera_id=?", (camera_id,)).fetchone()
        task = self.tasks.get(camera_id)
        if force_restart and task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.tasks.pop(camera_id, None)
            task = None
        if not config or not config["enabled"]:
            if task and not task.done():
                task.cancel()
            self.tasks.pop(camera_id, None)
            set_camera_state(camera_id, "disabled")
        elif self.detector is None:
            async with self._model_lock:
                if self.detector is None:
                    try:
                        self.detector = await asyncio.to_thread(self.detector_factory)
                    except Exception:
                        set_camera_state(camera_id, "failed", "CPU inference model could not be loaded")
                        return
            if self.detector is None:
                return
            set_camera_state(camera_id, "connecting")
            self.tasks[camera_id] = asyncio.create_task(self._run(camera_id), name=f"ai-detection-{camera_id}")
        elif task is None or task.done():
            set_camera_state(camera_id, "connecting")
            self.tasks[camera_id] = asyncio.create_task(self._run(camera_id), name=f"ai-detection-{camera_id}")

    async def _run(self, camera_id: str) -> None:
        while not self.stopping:
            try:
                with connect() as db:
                    config = db.execute("SELECT * FROM ai_camera_settings WHERE camera_id=? AND enabled=1", (camera_id,)).fetchone()
                if not config:
                    set_camera_state(camera_id, "disabled")
                    return
                source = _camera_source(camera_id, bool(config["use_substream"]))
                zones = json.loads(config["zones_json"])
                seen_frame = False
                async for frame in _raw_frames(source, float(config["analysis_fps"])):
                    if self.stopping:
                        return
                    seen_frame = True
                    observations = await asyncio.to_thread(self.detector.infer, frame, float(config["confidence_threshold"]))
                    observations = [item for item in observations if point_in_zones(item["box"], zones)]
                    record_observations(camera_id, observations)
                    set_camera_state(camera_id, "analyzed")
                if not seen_frame:
                    raise RuntimeError("Camera stream ended before an analysis frame was received")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = str(exc)[:240] or "Inference stream failed"
                set_camera_state(camera_id, "failed", reason)
                await asyncio.sleep(5)

