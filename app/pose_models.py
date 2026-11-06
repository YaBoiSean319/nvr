"""The pose model for fall detection (#230): body points for a person the NVR is already following.

The model is MediaPipe's Pose Landmarker "lite" (Apache-2.0, as published by the MediaPipe project; the licence of the trained weights is theirs and was not independently
verified here). Its download is a ``.task`` file (a zip of two TFLite models); only the landmark model is used, because the NVR has already found the person and a crop of them
is all it needs. OpenVINO reads the ``.tflite`` file directly.

The image downloads the bundle at build time and checks it against the pinned checksum (``Dockerfile``, kept the same as this registry by a test), and the extracted model is
checked again here before it is used. Nothing here needs a licence from anyone; anything that did (Ultralytics YOLO pose is AGPL-3.0 or paid) is deliberately not used.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .plate_models import resize_bilinear

logger = logging.getLogger(__name__)

MODELS_DIR = Path(os.environ.get("NVR_POSE_MODELS_DIR", "/opt/nvr/models/pose"))
INPUT_SIZE = 256
CROP_MARGIN = 1.25  # The crop is the person's longer side times this, so arms and legs are not cut off
MIN_POSE_SCORE = 0.3  # The model's own confidence that a person is in the crop; low enough to keep a person lying on the floor


@dataclass(frozen=True)
class ModelFile:
    name: str
    url: str
    sha256: str
    size_bytes: int


# The download (a zip of two models) and the one model used from it.
BUNDLE = ModelFile("pose_landmarker_lite.task", "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task",
                   "59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a", 5777746)
LANDMARKS = ModelFile("pose_landmarks_detector.tflite", BUNDLE.url, "ad6cfd3c903eb31a4ee788b809e45ecf9fa69923b69b9f3f2d9ae616ff433e58", 2818390)
SHIPPED = (BUNDLE,)

# The body points fall detection uses, by name, at their place among the model's 33 landmarks.
POINTS = {"nose": 0, "left_shoulder": 11, "right_shoulder": 12, "left_hip": 23, "right_hip": 24, "left_knee": 25, "right_knee": 26, "left_ankle": 27, "right_ankle": 28}
# Those and the arms, for drawing a skeleton. Fall detection keeps to POINTS: its decision looks at how wide the body is, and arms held out would change that.
DRAWN_POINTS = {**POINTS, "left_elbow": 13, "right_elbow": 14, "left_wrist": 15, "right_wrist": 16}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def file_ok(model: ModelFile, directory: Path | None = None) -> bool:
    """Whether the file is there and is the one that was pinned."""
    path = (directory or MODELS_DIR) / model.name
    try:
        return path.is_file() and path.stat().st_size == model.size_bytes and sha256_of(path) == model.sha256
    except OSError:
        return False


def installed(directory: Path | None = None) -> bool:
    return file_ok(LANDMARKS, directory)


def extract(bundle: Path, directory: Path) -> None:
    """Unpack the landmark model from the downloaded bundle into ``directory`` (the build does this; tests do too)."""
    import zipfile

    with zipfile.ZipFile(bundle) as archive:
        (directory / LANDMARKS.name).write_bytes(archive.read(LANDMARKS.name))


def status() -> dict[str, Any]:
    return {"installed": installed(), "model": "MediaPipe Pose Landmarker (lite)", "licence": "Apache-2.0 (as stated by the MediaPipe project)", "size_bytes": LANDMARKS.size_bytes}


def square_crop(frame: np.ndarray, box: dict[str, float]) -> tuple[np.ndarray, float, float, float]:
    """A square picture of the person (the frame padded with black where the square leaves it), and where it came from: (crop, left, top, side) in frame pixels."""
    rows, columns = frame.shape[:2]
    centre_x, centre_y = (box["x"] + box["width"] / 2) * columns, (box["y"] + box["height"] / 2) * rows
    side = max(box["width"] * columns, box["height"] * rows) * CROP_MARGIN
    side = max(side, 16.0)
    left, top = centre_x - side / 2, centre_y - side / 2
    size = int(round(side))
    canvas = np.zeros((size, size, frame.shape[2] if frame.ndim == 3 else 1), dtype=frame.dtype)
    x0, y0 = int(max(0, np.floor(left))), int(max(0, np.floor(top)))
    x1, y1 = int(min(columns, np.ceil(left + size))), int(min(rows, np.ceil(top + size)))
    if x1 > x0 and y1 > y0:
        dx, dy = x0 - int(np.floor(left)), y0 - int(np.floor(top))
        piece = frame[y0:y1, x0:x1].reshape(y1 - y0, x1 - x0, -1)
        canvas[dy:dy + piece.shape[0], dx:dx + piece.shape[1]] = piece[:size - dy, :size - dx]
    return canvas, float(np.floor(left)), float(np.floor(top)), float(size)


def _sigmoid(value: np.ndarray | float) -> np.ndarray | float:
    return 1.0 / (1.0 + np.exp(-value))


def decode(landmarks: np.ndarray, score: float, rows: int, columns: int, left: float, top: float, side: float, names: dict[str, int] | None = None) -> dict[str, tuple[float, float, float]] | None:
    """The body points from the model's output, as (x, y, visibility) in fractions of the frame; None when the model doubts a person is there.

    ``landmarks`` is the 195 numbers (39 landmarks of x, y, z, visibility, presence): x and y in the 256-pixel crop, visibility a logit."""
    if score < MIN_POSE_SCORE:
        return None
    points = np.asarray(landmarks, dtype=np.float32).reshape(-1, 5)
    found: dict[str, tuple[float, float, float]] = {}
    for name, index in (names or POINTS).items():
        x, y, _, visibility, _ = points[index]
        found[name] = (float(np.clip((left + x / INPUT_SIZE * side) / columns, -0.5, 1.5)), float(np.clip((top + y / INPUT_SIZE * side) / rows, -0.5, 1.5)), float(_sigmoid(visibility)))
    return found


class PoseReader:
    """The landmark model run with OpenVINO. ``pose`` takes a BGR frame and a person's box and returns that person's body points."""

    def __init__(self, provider: str = "CPU", core: Any = None, directory: Path | None = None) -> None:
        folder = directory or MODELS_DIR
        if core is None:
            import openvino as ov
            core = ov.Core()
        self.model = core.compile_model(core.read_model(str(folder / LANDMARKS.name)), provider)
        outputs = list(self.model.outputs)
        self._landmarks = next(output for output in outputs if list(output.shape)[-1] == 195)
        self._score = next(output for output in outputs if list(output.shape) == [1, 1])

    def pose(self, frame: np.ndarray, box: dict[str, float]) -> dict[str, tuple[float, float, float]] | None:
        """The body points fall detection decides on."""
        found = self.pose_full(frame, box)
        return None if found is None else {name: point for name, point in found.items() if name in POINTS}

    def pose_full(self, frame: np.ndarray, box: dict[str, float]) -> dict[str, tuple[float, float, float]] | None:
        """Those and the arms too, for drawing."""
        rows, columns = frame.shape[:2]
        crop, left, top, side = square_crop(frame, box)
        tensor = (resize_bilinear(crop[:, :, ::-1], INPUT_SIZE, INPUT_SIZE) / 255.0).astype(np.float32)[None]
        outputs = self.model(tensor)
        # The pose score is already a probability (about 0 for an empty picture); the landmarks' visibility is a logit, which ``decode`` turns into one.
        return decode(np.asarray(outputs[self._landmarks]), float(np.asarray(outputs[self._score]).reshape(-1)[0]), rows, columns, left, top, side, DRAWN_POINTS)


def load_reader(provider: str = "CPU") -> PoseReader | None:
    """The pose reader if the model is installed and loads, else None (no falls are then looked for)."""
    if not installed():
        return None
    try:
        return PoseReader(provider=provider)
    except Exception:  # noqa: BLE001 - a model that will not load must never stop detection
        logger.exception("The pose model could not be loaded")
        return None
