"""Licence plate models by region (#210): what ships in the image, which one is chosen, and the readers that run them.

Plates look different around the world, and a recogniser reads only what it was trained on. The image ships one plate **detector** (it finds a plate in a picture of any
country) and a **recogniser** for each popular region; the administrator chooses the region in Settings, Licence plates. A region's recogniser reads a cropped plate:

- ``europe``: ``european_mobile_vit_v2_ocr``, trained on European plates;
- ``worldwide``: ``global_mobile_vit_v2_ocr``, trained on Latin-alphabet plates from 65+ countries (the United States, Canada, Mexico, Australia, the United Kingdom
  and others);
- ``custom``: models the administrator installed beside the others (the paths in ``docs/licence-plates.md``), for example Intel's Open Model Zoo pair for Chinese plates.

The shipped models are from the MIT-licensed ``open-image-models`` and ``fast-plate-ocr`` projects (ONNX, read directly by OpenVINO). They are downloaded in the image
build with pinned checksums (``Dockerfile``, ``scripts/plate_model_files.py``) and verified again here before use. Their accuracy on a particular installation's cameras
is not known until measured: ``python -m app.plates evaluate``.

Contracts (from the projects' published source). **Detector**: input ``images`` ``[1,3,S,S]`` float32, RGB 0 to 1, the picture letter-boxed (grey 114 borders) to S x S; output
``[N,7]`` rows ``batch, x1, y1, x2, y2, class, score`` in the letter-boxed picture's pixels. **Recogniser**: input ``[N,H,W,C]`` uint8 (the model normalises it), the size,
colour mode and ``alphabet`` in its config file; the first output holds ``slots x len(alphabet)`` scores, each slot's best symbol read in order, trailing ``pad_char`` dropped.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("nvr.plates")

MODELS_DIR = Path(os.environ.get("NVR_PLATE_MODELS_DIR", "/opt/nvr/models/plates"))
DETECTOR_SIZE = 640
DETECTION_THRESHOLD = 0.35
LETTERBOX_GREY = 114
CUSTOM = "custom"
DEFAULT_REGION = "worldwide"


@dataclass(frozen=True)
class ModelFile:
    name: str  # The file's name in the models folder
    url: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class Region:
    id: str
    label: str
    covers: str
    recogniser: ModelFile | None = None
    config: ModelFile | None = None


_OIM = "https://github.com/ankandrew/open-image-models/releases/download/assets"
_FPO = "https://github.com/ankandrew/cnn-ocr-lp/releases/download/arg-plates"
DETECTOR_ID = "yolo-v9-t-640-license-plate-end2end"
DETECTOR = ModelFile("yolo-v9-t-640-license-plates-end2end.onnx", f"{_OIM}/yolo-v9-t-640-license-plates-end2end.onnx",
                     "c3c1026ca7d0585dd88084d68182dd897113712fa734ae1557ca70174440c076", 7_835_770)
EUROPE = Region(
    "europe", "Europe", "European plates (trained on plates from across Europe).",
    ModelFile("european_mobile_vit_v2_ocr.onnx", f"{_FPO}/european_mobile_vit_v2_ocr.onnx", "5f388f57ddec318d38d17e420d292f5a049595bec93f111838903f6617f6943f", 4_985_005),
    ModelFile("european_mobile_vit_v2_ocr_config.yaml", f"{_FPO}/european_mobile_vit_v2_ocr_config.yaml", "202b90f2a1f3a0d486b8f1fbd5cdc2f19f4f4881a094f086b983976ed3108aaf", 507))
WORLDWIDE = Region(
    "worldwide", "Worldwide, Latin alphabet (United States, Canada, Mexico, Australia, United Kingdom and 60+ more)",
    "Plates in the Latin alphabet from more than 65 countries, including the United States, Canada, Mexico, Australia and the United Kingdom.",
    ModelFile("global_mobile_vit_v2_ocr.onnx", f"{_FPO}/global_mobile_vit_v2_ocr.onnx", "e2fe5946c02e73901f73d21b3b7223a0c3e3b02059d4964fbf279b01af38d63c", 4_991_602),
    ModelFile("global_mobile_vit_v2_ocr_config.yaml", f"{_FPO}/global_mobile_vit_v2_ocr_config.yaml", "202b90f2a1f3a0d486b8f1fbd5cdc2f19f4f4881a094f086b983976ed3108aaf", 507))
CUSTOM_REGION = Region(CUSTOM, "Models I installed myself (for example Intel's Chinese-plate pair)",
                       "Whatever the plate models installed by hand cover: the detector and recogniser at the paths in docs/licence-plates.md.")
REGIONS: tuple[Region, ...] = (WORLDWIDE, EUROPE, CUSTOM_REGION)
SHIPPED: tuple[ModelFile, ...] = (DETECTOR, *[item for region in REGIONS for item in (region.recogniser, region.config) if item is not None])


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_ok(model: ModelFile, directory: Path | None = None) -> bool:
    """Whether the file is there and is the one that was pinned."""
    path = (directory or MODELS_DIR) / model.name
    try:
        return path.is_file() and path.stat().st_size == model.size_bytes and sha256_of(path) == model.sha256
    except OSError:
        return False


def region_by_id(region_id: str) -> Region | None:
    return next((region for region in REGIONS if region.id == region_id), None)


def installed(region: Region, directory: Path | None = None) -> bool:
    """Whether a region's models can be used: the shipped detector and recogniser files, or (custom) the hand-installed pair."""
    if region.id == CUSTOM:
        from . import plates
        return plates.models_available()
    return region.recogniser is not None and region.config is not None and all(file_ok(item, directory) for item in (DETECTOR, region.recogniser, region.config))


def status(directory: Path | None = None) -> list[dict[str, Any]]:
    """Each region with what it covers, whether its models are installed, and what they weigh."""
    out = []
    for region in REGIONS:
        size = None if region.recogniser is None else DETECTOR.size_bytes + region.recogniser.size_bytes + (region.config.size_bytes if region.config else 0)
        out.append({"id": region.id, "label": region.label, "covers": region.covers, "installed": installed(region, directory), "size_bytes": size, "manual": region.id == CUSTOM})
    return out


# --- the reader ---------------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SlotConfig:
    """What a fixed-slot recogniser's config file says."""
    slots: int
    alphabet: str
    pad_char: str
    height: int
    width: int
    grayscale: bool

    @classmethod
    def read(cls, path: Path) -> "SlotConfig":
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("The recogniser's config file is not a mapping")
        config = cls(int(data["max_plate_slots"]), str(data["alphabet"]), str(data["pad_char"]), int(data["img_height"]), int(data["img_width"]),
                     str(data.get("image_color_mode", "grayscale")) != "rgb")
        if config.pad_char not in config.alphabet or config.slots < 1 or len(config.pad_char) != 1:
            raise ValueError("The recogniser's config file is not usable")
        return config


def resize_bilinear(image: np.ndarray, height: int, width: int) -> np.ndarray:
    """A picture (rows x columns x channels) resized with bilinear sampling at pixel centres, averaging when it shrinks by more than two."""
    source = image.astype(np.float32)
    rows, columns = source.shape[:2]
    # Shrinking a lot: average whole blocks first so thin strokes are not lost.
    factor_y, factor_x = max(1, int(rows / height)), max(1, int(columns / width))
    if factor_y > 1 or factor_x > 1:
        trim_y, trim_x = rows - rows % factor_y, columns - columns % factor_x
        source = source[:trim_y, :trim_x].reshape(trim_y // factor_y, factor_y, trim_x // factor_x, factor_x, -1).mean(axis=(1, 3))
        rows, columns = source.shape[:2]
    y = np.clip((np.arange(height) + 0.5) * rows / height - 0.5, 0, rows - 1)
    x = np.clip((np.arange(width) + 0.5) * columns / width - 0.5, 0, columns - 1)
    y0, x0 = np.floor(y).astype(int), np.floor(x).astype(int)
    y1, x1 = np.minimum(y0 + 1, rows - 1), np.minimum(x0 + 1, columns - 1)
    fy, fx = (y - y0)[:, None, None], (x - x0)[None, :, None]
    top = source[y0][:, x0] * (1 - fx) + source[y0][:, x1] * fx
    bottom = source[y1][:, x0] * (1 - fx) + source[y1][:, x1] * fx
    return top * (1 - fy) + bottom * fy


def letterbox(frame: np.ndarray, size: int) -> tuple[np.ndarray, float, float, float]:
    """The picture scaled to fit a size x size square and centred on grey: (tensor [1,3,S,S] RGB 0..1, scale, left padding, top padding)."""
    rows, columns = frame.shape[:2]
    scale = min(size / rows, size / columns)
    new_h, new_w = max(1, int(round(rows * scale))), max(1, int(round(columns * scale)))
    resized = resize_bilinear(frame, new_h, new_w)
    canvas = np.full((size, size, 3), LETTERBOX_GREY, dtype=np.float32)
    top, left = (size - new_h) // 2, (size - new_w) // 2
    canvas[top:top + new_h, left:left + new_w] = resized
    return (canvas[:, :, ::-1].transpose(2, 0, 1)[None] / 255.0).astype(np.float32), scale, float(left), float(top)  # BGR to RGB


def decode_slots(scores: np.ndarray, config: SlotConfig) -> tuple[str, float]:
    """The text a fixed-slot recogniser's output spells and its confidence (the mean of the kept characters' scores; 0 for nothing)."""
    grid = np.asarray(scores, dtype=np.float32).reshape(-1, config.slots, len(config.alphabet))[0]
    best = grid.argmax(axis=1)
    strength = grid.max(axis=1)
    characters = [config.alphabet[index] for index in best]
    text = "".join(characters).rstrip(config.pad_char)
    kept = [float(strength[position]) for position, character in enumerate(characters[:len(text)]) if character != config.pad_char]
    return text.replace(config.pad_char, ""), (float(np.mean(kept)) if kept else 0.0)


class RegionReader:
    """The shipped plate detector and one region's recogniser, run with OpenVINO. ``read`` takes a BGR frame and returns the plates found, with their text."""

    def __init__(self, region: Region, provider: str = "CPU", core: Any = None, directory: Path | None = None) -> None:
        if region.recogniser is None or region.config is None:
            raise ValueError(f"{region.id} has no shipped models")
        folder = directory or MODELS_DIR
        if core is None:
            import openvino as ov
            core = ov.Core()
        self.region = region
        self.config = SlotConfig.read(folder / region.config.name)
        self.detector = core.compile_model(core.read_model(str(folder / DETECTOR.name)), provider)
        self.recogniser = core.compile_model(core.read_model(str(folder / region.recogniser.name)), provider)
        self.reader_id = f"{DETECTOR_ID}+{region.recogniser.name.removesuffix('.onnx')}"

    def detect(self, frame: np.ndarray) -> list[tuple[float, tuple[float, float, float, float]]]:
        """Plates as (score, (x1, y1, x2, y2)) in fractions of the frame."""
        rows, columns = frame.shape[:2]
        tensor, scale, left, top = letterbox(frame, DETECTOR_SIZE)
        found = np.asarray(next(iter(self.detector(tensor).values()))).reshape(-1, 7)
        plates = []
        for _, x1, y1, x2, y2, _, score in found:
            if score < DETECTION_THRESHOLD:
                continue
            box = ((x1 - left) / scale / columns, (y1 - top) / scale / rows, (x2 - left) / scale / columns, (y2 - top) / scale / rows)
            box = tuple(min(max(float(value), 0.0), 1.0) for value in box)
            if box[2] > box[0] and box[3] > box[1]:
                plates.append((float(score), box))
        return plates

    def recognise(self, crop: np.ndarray) -> tuple[str, float]:
        """The text on a cropped plate (a BGR picture) and the recogniser's confidence."""
        config = self.config
        if config.grayscale:
            picture = (crop[:, :, 0] * 0.114 + crop[:, :, 1] * 0.587 + crop[:, :, 2] * 0.299)[:, :, None]
        else:
            picture = crop[:, :, ::-1]
        tensor = np.clip(np.rint(resize_bilinear(picture, config.height, config.width)), 0, 255).astype(np.uint8)[None]
        outputs = self.recogniser(tensor)
        plate = next((value for key, value in outputs.items() if key.get_any_name() == "plate"), next(iter(outputs.values())))
        return decode_slots(np.asarray(plate), config)

    def read(self, frame: np.ndarray) -> list[Any]:
        from .plates import Plate, normalise, valid_plate
        height, width = frame.shape[:2]
        plates = []
        for detected, (x1, y1, x2, y2) in self.detect(frame):
            left, right = int(x1 * width), max(int(x1 * width) + 1, int(round(x2 * width)))
            top, bottom = int(y1 * height), max(int(y1 * height) + 1, int(round(y2 * height)))
            crop = frame[top:bottom, left:right]
            if crop.size == 0:
                continue
            text, confidence = self.recognise(crop)
            text = normalise(text)
            if not valid_plate(text):
                continue
            plates.append(Plate(text, round(min(1.0, detected, confidence), 3), {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}, crop))
        return plates
