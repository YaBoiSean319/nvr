"""Deterministic detection fixture: synthetic people and vehicles in generated footage.

Frames are drawn at the detector's input size (448x256 BGR). Each object is a flat,
saturated rectangle on a grey background, so ``SceneDetector`` can find it from the
pixels alone, after H.264 encoding too. Tests get repeatable detections and tracks
without depending on model accuracy.

The scene, sampled at ``FRAME_INTERVAL`` for ``FRAME_COUNT`` frames:

- ``red-person`` walks left to right along the upper path;
- ``green-person`` walks right to left slightly lower, crossing the red person
  around frame 17 (their boxes overlap there);
- ``blue-vehicle`` drives right to left along the lower path.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

import numpy as np

WIDTH = 448
HEIGHT = 256
FRAME_COUNT = 30
FRAME_INTERVAL = timedelta(seconds=0.5)
FRAME_BYTES = WIDTH * HEIGHT * 3
BACKGROUND = (110, 110, 110)


@dataclass(frozen=True)
class SceneObject:
    name: str
    label: str
    color: str
    bgr: tuple[int, int, int]
    width: int
    height: int
    start_x: int
    y: int
    step_x: int

    def box(self, frame: int) -> tuple[int, int, int, int]:
        x = self.start_x + self.step_x * frame
        return x, self.y, x + self.width, self.y + self.height


OBJECTS = (
    SceneObject("red-person", "person", "red", (30, 30, 220), 24, 64, 10, 40, 12),
    SceneObject("green-person", "person", "green", (30, 200, 30), 24, 64, 414, 60, -12),
    SceneObject("blue-vehicle", "vehicle", "blue", (220, 40, 30), 90, 40, 350, 170, -10),
)


def frame(index: int) -> bytes:
    image = np.empty((HEIGHT, WIDTH, 3), dtype=np.uint8)
    image[:, :] = BACKGROUND
    for item in OBJECTS:
        x1, y1, x2, y2 = item.box(index)
        image[y1:y2, x1:x2] = item.bgr
    return image.tobytes()


def frames() -> Iterator[bytes]:
    for index in range(FRAME_COUNT):
        yield frame(index)


def frame_times(start: datetime) -> list[datetime]:
    return [start + FRAME_INTERVAL * index for index in range(FRAME_COUNT)]


def write_footage(path: Path) -> Path:
    """Encode the scene as an H.264 MP4 at one frame per sample."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to generate fixture footage")
    rate = 1 / FRAME_INTERVAL.total_seconds()
    subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{WIDTH}x{HEIGHT}", "-r", f"{rate:g}", "-i", "pipe:0", "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", "12", "-pix_fmt", "yuv420p", "-g", "1", str(path)],
        input=b"".join(frames()), check=True,
    )
    return path


def decode_footage(path: Path) -> list[bytes]:
    """Decode footage to detector-sized BGR frames, as the AI worker's FFmpeg pipeline does."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to decode fixture footage")
    result = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-an",
         "-vf", f"scale={WIDTH}:{HEIGHT}:flags=bilinear", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"],
        capture_output=True, check=True,
    )
    data = result.stdout
    return [data[offset:offset + FRAME_BYTES] for offset in range(0, len(data) - FRAME_BYTES + 1, FRAME_BYTES)]


class SceneDetector:
    """Finds the scene's objects by colour; stands in for the OpenVINO detector."""

    def infer(self, bgr_frame: bytes, threshold: float) -> list[dict[str, object]]:
        image = np.frombuffer(bgr_frame, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3).astype(np.int16)
        found: list[dict[str, object]] = []
        for item in OBJECTS:
            mask = np.abs(image - np.array(item.bgr, dtype=np.int16)).sum(axis=2) < 90
            rows = np.flatnonzero(mask.sum(axis=1) >= 3)
            columns = np.flatnonzero(mask.sum(axis=0) >= 3)
            if rows.size == 0 or columns.size == 0 or mask.sum() < 50:
                continue
            x1, x2 = columns[0] / WIDTH, (columns[-1] + 1) / WIDTH
            y1, y2 = rows[0] / HEIGHT, (rows[-1] + 1) / HEIGHT
            confidence = 0.9
            if confidence < threshold:
                continue
            found.append({"label": item.label, "confidence": confidence,
                          "box": {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1},
                          "attributes": {"color": item.color}})
        return found
