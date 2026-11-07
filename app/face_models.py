"""Faces for the face watch (#253): find faces in a picture, and tell whether two faces are the same person.

Two models from OpenCV's model zoo, both with licences that allow commercial use and neither needing a licence from anyone: **YuNet** (``face_detection_yunet_2023mar``, MIT,
Shiqi Yu) finds faces and five landmarks on each, **SFace** (``face_recognition_sface_2021dec``, Apache-2.0) turns an aligned 112 x 112 face into a 128-number embedding. They
are ONNX files read directly by OpenVINO, downloaded in the image build with pinned checksums (``Dockerfile``, kept in step with this registry by a test) and verified again
before use. InsightFace's popular ArcFace weights were not used: they are for non-commercial research only.

What the licences do not say is what the models were trained on; neither project states it (SFace's paper used public research face datasets). That is a question for the owner's
counsel, as is whether recognising faces is allowed where the cameras are (``docs/face-watch.md``).

Contracts (from the models and OpenCV's reference implementation). **YuNet**: input ``input`` ``[1,3,640,640]`` float32, BGR 0 to 255 unscaled; outputs ``cls_S``, ``obj_S``, ``bbox_S``,
``kps_S`` for strides S of 8, 16 and 32 (one row per cell of a 640/S grid): score is ``sqrt(cls x obj)`` (each clamped to 0..1), the box is centred at ``(column + bbox[0], row + bbox[1])
x S`` with size ``exp(bbox[2]), exp(bbox[3]) x S``, and each landmark is ``(column + kps[2i], row + kps[2i+1]) x S`` for the right eye, left eye, nose and the right and left mouth
corners (the subject's right and left, so the first is on the left of the picture). **SFace**: input ``data`` ``[1,3,112,112]`` float32, RGB 0 to 255 unscaled, the face aligned to the
ArcFace template; output ``fc1`` ``[1,128]``. Two faces are compared by cosine similarity of their embeddings.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from .plate_models import ModelFile, file_ok

logger = logging.getLogger("nvr.faces")

MODELS_DIR = Path(os.environ.get("NVR_FACE_MODELS_DIR", "/opt/nvr/models/faces"))
_ZOO = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
DETECTOR_ID = "face_detection_yunet_2023mar"
RECOGNISER_ID = "face_recognition_sface_2021dec"
DETECTOR = ModelFile("face_detection_yunet_2023mar.onnx", f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
                     "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4", 232_589)
RECOGNISER = ModelFile("face_recognition_sface_2021dec.onnx", f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
                       "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79", 38_696_353)
SHIPPED: tuple[ModelFile, ...] = (DETECTOR, RECOGNISER)

DETECTOR_SIZE = 640
STRIDES = (8, 16, 32)
DETECTION_THRESHOLD = 0.7  # A face worth keeping (YuNet's own demo uses 0.9 for display; a watch should not act on a doubtful one)
NMS_IOU = 0.3
MIN_FACE_PIXELS = 32  # Narrower than this in the picture it is read from, there is too little of a face to tell people apart
ALIGNED_SIZE = 112
# Where SFace/ArcFace expects the five landmarks in the 112 x 112 aligned face (right eye, left eye, nose, right and left mouth corners).
TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float64)
# Cosine similarity at which two faces are called the same person. OpenCV recommends 0.363 for SFace; a watch that raises alarms starts stricter.
DEFAULT_MATCH_THRESHOLD = 0.45


def installed(directory: Path | None = None) -> bool:
    return all(file_ok(model, directory) for model in SHIPPED)


def status() -> dict[str, Any]:
    return {"available": installed(), "detector": DETECTOR_ID, "recogniser": RECOGNISER_ID,
            "detector_installed": file_ok(DETECTOR), "recogniser_installed": file_ok(RECOGNISER)}


@dataclass(frozen=True)
class Face:
    score: float
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 as fractions of the picture
    landmarks: tuple[tuple[float, float], ...]  # the five points, as fractions of the picture
    embedding: np.ndarray | None = field(default=None, compare=False, repr=False)
    pixels: tuple[int, int] = (0, 0)  # the face's width and height in the picture it was found in


def letterbox(frame: np.ndarray, size: int = DETECTOR_SIZE) -> tuple[np.ndarray, float]:
    """The picture scaled to fit a size x size square with its top left corner at the origin and black below and to the right: (tensor [1,3,S,S] BGR 0..255, scale)."""
    rows, columns = frame.shape[:2]
    scale = min(size / rows, size / columns)
    height, width = max(1, int(round(rows * scale))), max(1, int(round(columns * scale)))
    y = np.minimum(((np.arange(height) + 0.5) / scale).astype(int), rows - 1)
    x = np.minimum(((np.arange(width) + 0.5) / scale).astype(int), columns - 1)
    canvas = np.zeros((size, size, 3), dtype=np.float32)
    canvas[:height, :width] = frame[y][:, x]
    return canvas.transpose(2, 0, 1)[None], scale


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float = NMS_IOU) -> list[int]:
    """Indices of the boxes (x1, y1, x2, y2) kept, best first, dropping any that overlaps a better one by more than ``iou``."""
    order = list(np.argsort(-scores))
    keep: list[int] = []
    while order:
        best = order.pop(0)
        keep.append(best)
        rest = []
        for other in order:
            x1, y1 = max(boxes[best][0], boxes[other][0]), max(boxes[best][1], boxes[other][1])
            x2, y2 = min(boxes[best][2], boxes[other][2]), min(boxes[best][3], boxes[other][3])
            inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
            union = ((boxes[best][2] - boxes[best][0]) * (boxes[best][3] - boxes[best][1])
                     + (boxes[other][2] - boxes[other][0]) * (boxes[other][3] - boxes[other][1]) - inter)
            if union <= 0 or inter / union <= iou:
                rest.append(other)
        order = rest
    return keep


def decode_detections(outputs: dict[str, np.ndarray], threshold: float = DETECTION_THRESHOLD, size: int = DETECTOR_SIZE) -> list[tuple[float, np.ndarray, np.ndarray]]:
    """(score, box [x1,y1,x2,y2], landmarks [5,2]) in the letter-boxed picture's pixels, strongest first, overlapping ones dropped."""
    scores, boxes, points = [], [], []
    for stride in STRIDES:
        cells = size // stride
        cls = np.clip(np.asarray(outputs[f"cls_{stride}"], dtype=np.float32).reshape(-1), 0, 1)
        obj = np.clip(np.asarray(outputs[f"obj_{stride}"], dtype=np.float32).reshape(-1), 0, 1)
        bbox = np.asarray(outputs[f"bbox_{stride}"], dtype=np.float32).reshape(-1, 4)
        kps = np.asarray(outputs[f"kps_{stride}"], dtype=np.float32).reshape(-1, 10)
        score = np.sqrt(cls * obj)
        index = np.nonzero(score >= threshold)[0]
        for i in index:
            row, column = divmod(int(i), cells)
            cx, cy = (column + bbox[i, 0]) * stride, (row + bbox[i, 1]) * stride
            width, height = float(np.exp(bbox[i, 2])) * stride, float(np.exp(bbox[i, 3])) * stride
            scores.append(float(score[i]))
            boxes.append([cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2])
            points.append([[(column + kps[i, 2 * n]) * stride, (row + kps[i, 2 * n + 1]) * stride] for n in range(5)])
    if not scores:
        return []
    keep = nms(np.array(boxes), np.array(scores))
    return [(scores[i], np.array(boxes[i]), np.array(points[i])) for i in keep]


def similarity_transform(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """The 2 x 3 matrix of the rotation, uniform scale and shift that carries the points ``source`` closest to ``target`` (Umeyama, least squares)."""
    count = len(source)
    source_mean, target_mean = source.mean(axis=0), target.mean(axis=0)
    source_centred, target_centred = source - source_mean, target - target_mean
    u, singular, vt = np.linalg.svd(target_centred.T @ source_centred / count)
    sign = np.diag([1.0, -1.0 if np.linalg.det(u) * np.linalg.det(vt) < 0 else 1.0])
    rotation = u @ sign @ vt
    scale = float(np.trace(np.diag(singular) @ sign) / ((source_centred ** 2).sum() / count))
    return np.hstack([scale * rotation, (target_mean - scale * rotation @ source_mean)[:, None]])


def warp(image: np.ndarray, matrix: np.ndarray, size: int = ALIGNED_SIZE) -> np.ndarray:
    """The picture carried through ``matrix`` (2 x 3, source to result) into a size x size picture, sampled bilinearly (the edge repeated where it runs out)."""
    linear, shift = matrix[:, :2], matrix[:, 2]
    inverse = np.linalg.inv(linear)
    ys, xs = np.mgrid[0:size, 0:size]
    destination = np.stack([xs.ravel(), ys.ravel()]).astype(np.float64)
    source = inverse @ (destination - shift[:, None])
    rows, columns = image.shape[:2]
    x = np.clip(source[0], 0, columns - 1)
    y = np.clip(source[1], 0, rows - 1)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    x1, y1 = np.minimum(x0 + 1, columns - 1), np.minimum(y0 + 1, rows - 1)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]
    pixels = image.astype(np.float32)
    top = pixels[y0, x0] * (1 - fx) + pixels[y0, x1] * fx
    bottom = pixels[y1, x0] * (1 - fx) + pixels[y1, x1] * fx
    return (top * (1 - fy) + bottom * fy).reshape(size, size, -1)


def align(frame: np.ndarray, landmarks_px: np.ndarray) -> np.ndarray:
    """The face in ``frame`` with these five landmarks (pixels), turned, scaled and cut to the 112 x 112 SFace expects (BGR float)."""
    return warp(frame, similarity_transform(np.asarray(landmarks_px, dtype=np.float64), TEMPLATE))


def normalise(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float64).reshape(-1)
    length = float(np.linalg.norm(vector))
    return vector / length if length > 0 else vector


def cosine(left: np.ndarray, right: np.ndarray) -> float:
    return float(np.dot(normalise(left), normalise(right)))


def match(embedding: np.ndarray, entries: Iterable[tuple[str, Sequence[np.ndarray]]], threshold: float = DEFAULT_MATCH_THRESHOLD) -> tuple[str, float] | None:
    """The watch entry (id, embeddings) the face is most like, with how alike, when that is at least ``threshold``; None otherwise."""
    best: tuple[str, float] | None = None
    for entry_id, embeddings in entries:
        for known in embeddings:
            similarity = cosine(embedding, known)
            if best is None or similarity > best[1]:
                best = (entry_id, similarity)
    return best if best is not None and best[1] >= threshold else None


class FaceReader:
    """OpenVINO face detector and recogniser. ``faces`` takes a BGR picture (rows x columns x 3) and returns the faces found, each with its embedding."""

    reader_id = f"{DETECTOR_ID}+{RECOGNISER_ID}"

    def __init__(self, detector: Path | None = None, recogniser: Path | None = None, provider: str = "CPU", core: Any = None) -> None:
        if core is None:
            import openvino as ov
            core = ov.Core()
        self.detector = core.compile_model(core.read_model(str(detector or MODELS_DIR / DETECTOR.name)), provider)
        self.recogniser = core.compile_model(core.read_model(str(recogniser or MODELS_DIR / RECOGNISER.name)), provider)

    @staticmethod
    def _named(result: dict[Any, np.ndarray]) -> dict[str, np.ndarray]:
        # An output can have several names (OpenVINO keeps the ONNX name and the graph's own number): every one of them finds it.
        named: dict[str, np.ndarray] = {}
        for key, value in result.items():
            for name in ([key] if isinstance(key, str) else key.get_names()):
                named[name] = value
        return named

    def detect(self, frame: np.ndarray, threshold: float = DETECTION_THRESHOLD) -> list[Face]:
        """The faces in the picture (no embeddings), strongest first."""
        rows, columns = frame.shape[:2]
        tensor, scale = letterbox(frame)
        found = []
        for score, box, points in decode_detections(self._named(self.detector(tensor)), threshold):
            box_px, points_px = box / scale, points / scale
            x1, y1 = max(0.0, float(box_px[0])), max(0.0, float(box_px[1]))
            x2, y2 = min(float(columns), float(box_px[2])), min(float(rows), float(box_px[3]))
            if x2 - x1 < 1 or y2 - y1 < 1:
                continue
            found.append(Face(round(score, 4), (x1 / columns, y1 / rows, x2 / columns, y2 / rows),
                              tuple((float(x) / columns, float(y) / rows) for x, y in points_px), None, (int(x2 - x1), int(y2 - y1))))
        return found

    def embed(self, frame: np.ndarray, face: Face) -> np.ndarray:
        """The face's 128-number embedding (unit length), from the picture it was found in."""
        rows, columns = frame.shape[:2]
        points = np.array([[x * columns, y * rows] for x, y in face.landmarks])
        aligned = align(frame, points)[:, :, ::-1]  # BGR to RGB
        tensor = aligned.transpose(2, 0, 1)[None].astype(np.float32)
        return normalise(np.asarray(next(iter(self.recogniser(tensor).values()))))

    def faces(self, frame: np.ndarray, threshold: float = DETECTION_THRESHOLD, minimum_pixels: int = MIN_FACE_PIXELS) -> list[Face]:
        """The faces worth recognising (sharp enough detection, wide enough in the picture), each with its embedding."""
        result = []
        for face in self.detect(frame, threshold):
            if face.pixels[0] < minimum_pixels:
                continue
            result.append(Face(face.score, face.box, face.landmarks, self.embed(frame, face), face.pixels))
        return result


def load_reader(provider: str = "CPU") -> FaceReader | None:
    """The face reader if both models are installed and load, else None (no face is then looked for)."""
    if not installed():
        return None
    try:
        return FaceReader(provider=provider)
    except Exception:  # noqa: BLE001 - models that will not load must never stop detection
        logger.exception("The face models could not be loaded")
        return None
