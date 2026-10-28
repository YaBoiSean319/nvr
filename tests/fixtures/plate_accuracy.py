"""Accuracy of the plate pipeline on a labelled sample of rendered plates, at several levels of picture quality.

``python -m tests.fixtures.plate_accuracy`` prints the table in docs/licence-plates.md. The recogniser is the template-matching stand-in of
``tests.fixtures.plate_models`` run by the real PlateReader on the real OpenVINO runtime: it measures the pipeline (crop, resize, recognise, CTC decode, normalise,
look-alike folding), not the Open Model Zoo models, whose accuracy on a real camera is a post-merge check.
"""
from __future__ import annotations

import csv
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from app import plates
from tests.fixtures import plate_models as models

LEVELS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("Clean", {}),
    ("Noise (sigma 35)", {"noise": 35.0}),
    ("Noise (sigma 70)", {"noise": 70.0}),
    ("Plate 2 px off, noise 35", {"noise": 35.0, "shift": 2}),
    ("Plate 4 px off, noise 35", {"noise": 35.0, "shift": 4}),
    ("Blur (radius 5), noise 35", {"noise": 35.0, "blur": 5}),
    ("Low contrast (25%), noise 35", {"noise": 35.0, "contrast": 0.25}),
    ("Blur 4, low contrast 35%, noise 60, 2 px off", {"noise": 60.0, "blur": 4, "contrast": 0.35, "shift": 2}),
)


def sample(count: int, seed: int) -> list[str]:
    rng = np.random.default_rng(seed)
    return [models.random_text(rng) for _ in range(count)]


def measure(reader: plates.PlateReader, texts: list[str], seed: int = 7) -> list[tuple[str, dict[str, Any]]]:
    report = []
    for name, options in LEVELS:
        rng = np.random.default_rng(seed)
        pairs = []
        for text in texts:
            found = reader.read(models.frame_with(models.degrade(models.render(text), rng, **options)))
            best = max(found, key=lambda plate: plate.confidence, default=None)
            pairs.append((text, best.text if best else None))
        report.append((name, plates.score(pairs)))
    return report


def write_images(texts: list[str], directory: Path, seed: int = 7) -> Path:
    """The sample as PPM pictures and a labels CSV, for ``plates.evaluate`` (clean plates)."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "labels.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["image", "plate"])
        for index, text in enumerate(texts):
            frame = models.frame_with(models.render(text))
            (directory / f"{index:04d}.ppm").write_bytes(f"P6\n{frame.shape[1]} {frame.shape[0]}\n255\n".encode() + frame[:, :, ::-1].tobytes())
            writer.writerow([f"{index:04d}.ppm", text])
    return directory / "labels.csv"


def table(report: list[tuple[str, dict[str, Any]]]) -> str:
    lines = ["| Picture quality | Plates | Read | Whole plate right | Right ignoring look-alikes | Characters right |", "|---|---|---|---|---|---|"]
    for name, row in report:
        lines.append(f"| {name} | {row['plates']} | {row['read']} | {row['exact_percent']}% | {row['lookalike_tolerant_percent']}% | {row['character_percent']}% |")
    return "\n".join(lines)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        detector, recogniser = models.build_models(Path(directory))
        print(table(measure(plates.PlateReader(detector, recogniser), sample(500, 20260101))))
