"""What a tracked person or vehicle looks like, for searching across cameras.

Attributes are worked out by the AI worker for each detection and kept with the track:

- **colours**, from the pixels of the box, classified into a small palette: a person's upper body (the torso) and lower body (the legs), a
  vehicle's body. Each region's dominant palette colour is taken, so a flat red jacket is "red" whatever its exact shade;
- **vehicle type** (car, bus, truck, van) and a second opinion on the vehicle's colour, from an OpenVINO attribute model
  (``vehicle-attributes-recognition-barrier-0039``) when its files are installed. Without it the type is unknown and a search by type
  matches nothing: the colour comes from the pixels alone;
- **direction of travel** across the frame, from where the track began to where it is now: left, right, toward the camera (down the
  frame), away (up the frame), or stationary;
- **zone**: a 6x6 grid of the places the track's centre passed through, so a search for "the left half" can be answered from the track.

A track keeps the value most of its detections agreed on (ties go to the earlier). Nothing here identifies a person: it is what an
observer would say at a glance, and as reliable as the pixels are.
"""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from .detection import MODEL_HEIGHT, MODEL_WIDTH

PALETTE = ("black", "white", "gray", "red", "orange", "yellow", "green", "blue", "purple", "pink", "brown")
VEHICLE_TYPES = ("car", "bus", "truck", "van")
DIRECTIONS = ("left", "right", "toward", "away", "stationary")
GRID = 6
STATIONARY_BELOW = 0.08  # Net movement, as a fraction of the frame, under which a track has not gone anywhere.
MIN_REGION_PIXELS = 4

ATTRIBUTE_MODEL_ID = "vehicle-attributes-recognition-barrier-0039"
ATTRIBUTE_MODEL_PATH = Path(os.environ.get("NVR_AI_ATTRIBUTE_MODEL_PATH", f"/opt/nvr/models/{ATTRIBUTE_MODEL_ID}.xml"))

_PERSON_REGIONS = {"upper_colour": (0.2, 0.8, 0.18, 0.5), "lower_colour": (0.2, 0.8, 0.55, 0.9)}  # x0, x1, y0, y1 as fractions of the box
_VEHICLE_REGION = (0.15, 0.85, 0.25, 0.8)


def classify_pixels(pixels: np.ndarray) -> str | None:
    """The palette colour most of these BGR pixels (an N x 3 array) fall into, or None when there are none."""
    if pixels.size == 0:
        return None
    blue, green, red = (pixels[:, index].astype(np.float32) / 255.0 for index in range(3))
    top, bottom = np.maximum(np.maximum(blue, green), red), np.minimum(np.minimum(blue, green), red)
    spread = top - bottom
    saturation = np.divide(spread, top, out=np.zeros_like(top), where=top > 0)
    safe = np.where(spread > 0, spread, 1.0)
    hue = np.where(top == red, ((green - blue) / safe) % 6, np.where(top == green, (blue - red) / safe + 2, (red - green) / safe + 4)) * 60.0
    names = np.empty(top.shape, dtype=object)
    dark, pale = top < 0.2, (saturation < 0.18) & (top >= 0.2)
    colour = ~dark & ~pale
    names[dark] = "black"
    names[pale & (top > 0.75)] = "white"
    names[pale & (top <= 0.75)] = "gray"
    # Dark oranges are brown, so brown is taken before orange.
    bands = (
        ("red", (hue < 15) | (hue >= 345)), ("brown", (hue >= 15) & (hue < 45) & (top < 0.6)), ("orange", (hue >= 15) & (hue < 40)),
        ("yellow", (hue >= 40) & (hue < 70)), ("green", (hue >= 70) & (hue < 165)), ("blue", (hue >= 165) & (hue < 260)),
        ("purple", (hue >= 260) & (hue < 300)), ("pink", (hue >= 300) & (hue < 345)),
    )
    unassigned = colour.copy()
    for name, band in bands:
        chosen = unassigned & band
        names[chosen] = name
        unassigned &= ~band
    counts = Counter(names.tolist())
    return counts.most_common(1)[0][0]


def _crop_image(frame: bytes | None, box: dict[str, float], region: tuple[float, float, float, float]) -> np.ndarray:
    """A region, given as fractions of the box, cut from a detector-sized frame: rows x columns x 3 (BGR), possibly empty."""
    if frame is None or len(frame) != MODEL_WIDTH * MODEL_HEIGHT * 3:
        return np.empty((0, 0, 3), dtype=np.uint8)
    image = np.frombuffer(frame, dtype=np.uint8).reshape(MODEL_HEIGHT, MODEL_WIDTH, 3)
    x0, x1, y0, y1 = region
    left, top = box["x"] + box["width"] * x0, box["y"] + box["height"] * y0
    right, bottom = box["x"] + box["width"] * x1, box["y"] + box["height"] * y1
    columns = (max(0, int(left * MODEL_WIDTH)), min(MODEL_WIDTH, max(int(left * MODEL_WIDTH) + 1, int(round(right * MODEL_WIDTH)))))
    rows = (max(0, int(top * MODEL_HEIGHT)), min(MODEL_HEIGHT, max(int(top * MODEL_HEIGHT) + 1, int(round(bottom * MODEL_HEIGHT)))))
    return image[rows[0]:rows[1], columns[0]:columns[1]]


def _crop(frame: bytes | None, box: dict[str, float], region: tuple[float, float, float, float]) -> np.ndarray:
    """The pixels (N x 3, BGR) of a region, given as fractions of the box, inside a detector-sized frame."""
    return _crop_image(frame, box, region).reshape(-1, 3)


def describe_colours(frame: bytes | None, label: str, box: dict[str, float]) -> dict[str, str]:
    """The colours of one detection: a person's ``upper_colour`` and ``lower_colour``, or a vehicle's ``vehicle_colour``."""
    if label == "person":
        found = {name: classify_pixels(pixels) for name, region in _PERSON_REGIONS.items() if (pixels := _crop(frame, box, region)).shape[0] >= MIN_REGION_PIXELS}
    elif label == "vehicle":
        pixels = _crop(frame, box, _VEHICLE_REGION)
        found = {"vehicle_colour": classify_pixels(pixels)} if pixels.shape[0] >= MIN_REGION_PIXELS else {}
    else:
        found = {}
    return {name: value for name, value in found.items() if value}


class VehicleAttributeModel:
    """OpenVINO ``vehicle-attributes-recognition-barrier-0039``: a vehicle crop (72x72, BGR) in; a colour and a type out.

    The model's ``color`` output scores white, gray, yellow, red, green, blue, black, and its ``type`` output car, bus, truck, van.
    """

    COLOURS = ("white", "gray", "yellow", "red", "green", "blue", "black")
    SIZE = 72

    def __init__(self, path: Path = ATTRIBUTE_MODEL_PATH, provider: str = "CPU", core: Any = None) -> None:
        if core is None:
            import openvino as ov
            core = ov.Core()
        self.compiled = core.compile_model(core.read_model(str(path)), provider)

    def classify(self, frame: bytes | None, box: dict[str, float]) -> dict[str, str]:
        image = _crop_image(frame, box, (0.0, 1.0, 0.0, 1.0))
        if image.shape[0] * image.shape[1] < MIN_REGION_PIXELS:
            return {}
        rows = np.minimum((np.arange(self.SIZE) * image.shape[0] / self.SIZE).astype(int), image.shape[0] - 1)
        columns = np.minimum((np.arange(self.SIZE) * image.shape[1] / self.SIZE).astype(int), image.shape[1] - 1)
        tensor = image[rows][:, columns].transpose(2, 0, 1)[None].astype(np.float32)
        result = self.compiled(tensor)
        scores = {output.get_any_name(): np.asarray(value).reshape(-1) for output, value in result.items()}
        found: dict[str, str] = {}
        if "color" in scores and scores["color"].size == len(self.COLOURS):
            found["vehicle_colour"] = self.COLOURS[int(scores["color"].argmax())]
        if "type" in scores and scores["type"].size == len(VEHICLE_TYPES):
            found["vehicle_type"] = VEHICLE_TYPES[int(scores["type"].argmax())]
        return found


def load_attribute_model(path: Path = ATTRIBUTE_MODEL_PATH, provider: str = "CPU") -> VehicleAttributeModel | None:
    """The vehicle attribute model if its files are installed and it loads, else None (vehicle type is then not known)."""
    if not path.is_file() or not path.with_suffix(".bin").is_file():
        return None
    try:
        return VehicleAttributeModel(path, provider)
    except Exception:  # noqa: BLE001 - an attribute model that will not load must never stop detection
        return None


def attribute_model_available(path: Path = ATTRIBUTE_MODEL_PATH) -> bool:
    return path.is_file() and path.with_suffix(".bin").is_file()


def describe(frame: bytes | None, detections: list[dict[str, Any]], model: Any = None) -> None:
    """Attach each detection's attributes (``item["attributes"]``) from the frame it was found in."""
    for item in detections:
        try:
            attributes = describe_colours(frame, str(item["label"]), item["box"])
        except Exception:  # noqa: BLE001 - a detection whose pixels cannot be read is still tracked, just without colours
            attributes = {}
        if model is not None and item["label"] == "vehicle":
            try:
                attributes.update(model.classify(frame, item["box"]))
            except Exception:  # noqa: BLE001 - one crop the model cannot read leaves that detection with its pixel colours
                pass
        item["attributes"] = {**(item.get("attributes") or {}), **attributes}


def cell_mask(box: dict[str, float]) -> int:
    """The grid cell the centre of a box is in, as a single bit."""
    column = min(GRID - 1, max(0, int((box["x"] + box["width"] / 2) * GRID)))
    row = min(GRID - 1, max(0, int((box["y"] + box["height"] / 2) * GRID)))
    return 1 << (row * GRID + column)


def zone_cells(zone: dict[str, float]) -> int:
    """Every grid cell a rectangle (x, y, width, height as fractions of the frame) touches."""
    mask = 0
    for row in range(GRID):
        for column in range(GRID):
            if zone["x"] < (column + 1) / GRID and zone["x"] + zone["width"] > column / GRID and zone["y"] < (row + 1) / GRID and zone["y"] + zone["height"] > row / GRID:
                mask |= 1 << (row * GRID + column)
    return mask


def centre_in(box: dict[str, float], zone: dict[str, float]) -> bool:
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    return zone["x"] <= cx <= zone["x"] + zone["width"] and zone["y"] <= cy <= zone["y"] + zone["height"]


def direction_of(start: tuple[float, float], end: tuple[float, float]) -> str:
    dx, dy = end[0] - start[0], end[1] - start[1]
    if max(abs(dx), abs(dy)) < STATIONARY_BELOW:
        return "stationary"
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "toward" if dy > 0 else "away"


class TrackProfile:
    """What one live track has shown so far: its attribute votes, where it began, where it is, and where it has been."""

    def __init__(self) -> None:
        self.votes: dict[str, Counter[str]] = {}
        self.first: tuple[float, float] | None = None
        self.last: tuple[float, float] | None = None
        self.mask = 0
        self.seen = 0
        self.stored: dict[str, Any] | None = None

    def add(self, attributes: dict[str, str], box: dict[str, float]) -> None:
        for name, value in attributes.items():
            if name in {"upper_colour", "lower_colour", "vehicle_colour", "vehicle_type"} and value:
                self.votes.setdefault(name, Counter())[value] += 1
        centre = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        self.first = self.first or centre
        self.last = centre
        self.seen += 1
        self.mask |= cell_mask(box)

    def summary(self) -> dict[str, Any]:
        """The track's columns: the value each attribute's detections agreed on most (the earlier of a tie), its direction and its grid."""
        found: dict[str, Any] = {name: None for name in ("upper_colour", "lower_colour", "vehicle_colour", "vehicle_type")}
        for name, counts in self.votes.items():
            best = max(counts.values())
            found[name] = next(value for value in counts if counts[value] == best)  # Counter keeps first-seen order, so a tie goes to the earlier.
        found["direction"] = direction_of(self.first, self.last) if self.first and self.last and self.seen >= 2 else None  # One sighting says nothing about movement
        found["zone_mask"] = self.mask
        return found
