"""Stand-in plate models for tests and for measuring the plate pipeline, built as real OpenVINO models.

They are **not** the Open Model Zoo models and say nothing about how those perform on a camera. They exist so the real ``PlateReader`` (model loading, input
shapes, charset file, CTC decoding, normalising) runs against the real OpenVINO runtime, and so a labelled sample of rendered plates can be put through it:

- the *detector* answers one fixed plate box whatever the picture (so what is measured is reading, not finding);
- the *recogniser* is template matching: the plate crop is cut into seven character cells, each cell is compared (cosine similarity) with a template of
  every character, and the best matches are emitted as CTC scores with a blank between characters, as a real recogniser emits them.

Plates are drawn with FFmpeg's ``drawtext`` (so FFmpeg and a TrueType font are needed; ``available()`` says whether they are there).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np

from app import plates

SYMBOLS = plates.DEFAULT_CHARSET  # 0-9, A-Z
CELL = 39  # Pixels of one character cell in a rendered plate: 3 plate pixels to every pixel of the recogniser's input, so a cell is 13 of them
MARGIN = 3
PLATE_W, PLATE_H = 282, 72
FRAME_W, FRAME_H = 1280, 720
PLATE_LEFT, PLATE_TOP = 512, 432
PLATE_BOX = (PLATE_LEFT / FRAME_W, PLATE_TOP / FRAME_H, (PLATE_LEFT + PLATE_W) / FRAME_W, (PLATE_TOP + PLATE_H) / FRAME_H)
LENGTH = 7  # Characters on a rendered plate
INPUT_H, INPUT_W, CELL_W = 24, 94, 13  # The recogniser's input, and the width of one cell in it
FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")


def _font() -> str | None:
    return next((font for font in FONTS if Path(font).is_file()), None)


def available() -> bool:
    if shutil.which("ffmpeg") is None or _font() is None:
        return False
    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, check=False).stdout
    return "drawtext" in filters


_ATLAS: dict[str, np.ndarray] | None = None


def atlas() -> dict[str, np.ndarray]:
    """Every character drawn black on white in a cell, as float grey (PLATE_H x CELL), from one FFmpeg run."""
    global _ATLAS
    if _ATLAS is None:
        draw = ",".join(f"drawtext=fontfile={_font()}:text='{symbol}':fontsize=56:fontcolor=black:x={index * CELL}+({CELL}-text_w)/2:y=(h-text_h)/2" for index, symbol in enumerate(SYMBOLS))
        width = CELL * len(SYMBOLS)
        data = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=white:s={width}x{PLATE_H}", "-vf", draw, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                              capture_output=True, check=True, timeout=60).stdout
        strip = np.frombuffer(data, dtype=np.uint8).reshape(PLATE_H, width).astype(np.float32)
        _ATLAS = {symbol: strip[:, index * CELL:(index + 1) * CELL] for index, symbol in enumerate(SYMBOLS)}
    return _ATLAS


def render(text: str) -> np.ndarray:
    """A clean plate (PLATE_H x PLATE_W, grey 0-255): the characters in cells, black on white."""
    plate = np.full((PLATE_H, PLATE_W), 255.0, dtype=np.float32)
    for index, symbol in enumerate(text):
        left = MARGIN + index * CELL
        plate[:, left:left + CELL] = np.minimum(plate[:, left:left + CELL], atlas()[symbol])
    return plate


def degrade(plate: np.ndarray, rng: np.random.Generator, *, noise: float = 0.0, contrast: float = 1.0, blur: int = 0, shift: int = 0) -> np.ndarray:
    """What a camera does to a plate: it moves a few pixels, blurs, loses contrast (the plate goes grey) and adds noise."""
    result = plate.copy()
    if shift:
        dx = int(rng.integers(-shift, shift + 1))
        result = np.roll(result, dx, axis=1)
        if dx > 0:
            result[:, :dx] = 255.0
        elif dx < 0:
            result[:, dx:] = 255.0
    if blur:
        kernel = np.ones(2 * blur + 1, dtype=np.float32) / (2 * blur + 1)
        padded = np.pad(result, blur, mode="edge")
        result = np.apply_along_axis(lambda row: np.convolve(row, kernel, mode="valid"), 1, padded)[blur:-blur]
        result = np.apply_along_axis(lambda column: np.convolve(column, kernel, mode="valid"), 0, np.pad(result, ((blur, blur), (0, 0)), mode="edge"))
    result = 255.0 - contrast * (255.0 - result)
    if noise:
        result = result + rng.normal(0.0, noise, size=result.shape)
    return np.clip(result, 0, 255)


def frame_with(plate: np.ndarray) -> np.ndarray:
    """A full-size BGR camera frame with the plate on a vehicle-grey background where the stand-in detector looks."""
    frame = np.full((FRAME_H, FRAME_W, 3), 90, dtype=np.uint8)
    frame[PLATE_TOP:PLATE_TOP + PLATE_H, PLATE_LEFT:PLATE_LEFT + PLATE_W] = np.repeat(plate.astype(np.uint8)[:, :, None], 3, axis=2)
    return frame


def random_text(rng: np.random.Generator) -> str:
    return "".join(SYMBOLS[int(index)] for index in rng.integers(0, len(SYMBOLS), size=LENGTH))


def _templates() -> np.ndarray:
    """Zero-mean, unit-length templates (cell pixels x characters), made the way the recogniser sees a plate: resized to its input, then one cell cut out."""
    columns = []
    for symbol in SYMBOLS:
        plate = np.full((PLATE_H, PLATE_W), 255.0, dtype=np.float32)
        plate[:, MARGIN:MARGIN + CELL] = atlas()[symbol]
        small = plates._resize(np.repeat(plate[:, :, None], 3, axis=2), INPUT_H, INPUT_W).mean(axis=2)
        cell = small[:, 1:1 + CELL_W].reshape(-1)
        cell = cell - cell.mean()
        columns.append(cell / (np.linalg.norm(cell) + 1e-6))
    return np.stack(columns, axis=1).astype(np.float32)


def build_models(directory: Path) -> tuple[Path, Path]:
    """Write the stand-in detector and recogniser (OpenVINO IR) and the recogniser's charset file into a directory; return their .xml paths."""
    import openvino as ov
    from openvino import opset13 as ops

    # Detector: one plate, label 2, always in the same place, as [1,1,1,7] (image_id, label, confidence, x1, y1, x2, y2).
    image = ops.parameter([1, 3, plates.DETECTOR_SIZE, plates.DETECTOR_SIZE], np.float32, name="data")
    nothing = ops.multiply(ops.reduce_sum(image, ops.constant(np.array([0, 1, 2, 3], dtype=np.int64)), keep_dims=False), ops.constant(np.float32(0.0)))
    row = ops.constant(np.array([0, plates.PLATE_LABEL, 0.95, *PLATE_BOX], dtype=np.float32))
    detection = ops.reshape(ops.add(row, nothing), ops.constant(np.array([1, 1, 1, 7], dtype=np.int64)), False)
    detector = ov.Model([ops.result(detection, name="detection_out")], [image], "plate_detector")

    # Recogniser: grey -> seven cells -> normalise -> cosine with every template -> softmax -> blanks between characters.
    crop = ops.parameter([1, 3, INPUT_H, INPUT_W], np.float32, name="data")
    grey = ops.reduce_mean(crop, ops.constant(np.array([1], dtype=np.int64)), keep_dims=False)  # 1 x H x W
    cells = ops.strided_slice(grey, ops.constant(np.array([0, 0, 1], dtype=np.int64)), ops.constant(np.array([1, INPUT_H, 1 + LENGTH * CELL_W], dtype=np.int64)),
                              ops.constant(np.array([1, 1, 1], dtype=np.int64)), [0, 0, 0], [0, 0, 0])
    cells = ops.reshape(cells, ops.constant(np.array([INPUT_H, LENGTH, CELL_W], dtype=np.int64)), False)
    cells = ops.reshape(ops.transpose(cells, ops.constant(np.array([1, 0, 2], dtype=np.int64))), ops.constant(np.array([LENGTH, INPUT_H * CELL_W], dtype=np.int64)), False)
    centred = ops.subtract(cells, ops.reduce_mean(cells, ops.constant(np.array([1], dtype=np.int64)), keep_dims=True))
    length = ops.add(ops.sqrt(ops.reduce_sum(ops.multiply(centred, centred), ops.constant(np.array([1], dtype=np.int64)), keep_dims=True)), ops.constant(np.float32(1e-3)))
    similarity = ops.matmul(ops.divide(centred, length), ops.constant(_templates()), False, False)  # LENGTH x characters
    logits = ops.multiply(similarity, ops.constant(np.float32(40.0)))
    blank_column = ops.constant(np.full((LENGTH, 1), 5.0, dtype=np.float32))
    letters = ops.softmax(ops.concat([logits, blank_column], 1), 1)  # LENGTH x (characters + blank)
    blanks = ops.constant(np.tile(np.eye(len(SYMBOLS) + 1, dtype=np.float32)[-1], (LENGTH, 1)))
    interleaved = ops.concat([ops.unsqueeze(letters, ops.constant(np.array([1], dtype=np.int64))), ops.unsqueeze(blanks, ops.constant(np.array([1], dtype=np.int64)))], 1)
    scores = ops.reshape(interleaved, ops.constant(np.array([LENGTH * 2, 1, len(SYMBOLS) + 1], dtype=np.int64)), False)
    recogniser = ov.Model([ops.result(scores, name="scores")], [crop], "plate_recogniser")

    directory.mkdir(parents=True, exist_ok=True)
    detector_path, recogniser_path = directory / "plate-detector.xml", directory / "plate-recogniser.xml"
    ov.save_model(detector, str(detector_path), compress_to_fp16=False)
    ov.save_model(recogniser, str(recogniser_path), compress_to_fp16=False)
    recogniser_path.with_suffix(".charset.txt").write_text("\n".join([*SYMBOLS, "<blank>"]) + "\n", encoding="utf-8")
    return detector_path, recogniser_path
