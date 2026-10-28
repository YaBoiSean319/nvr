"""Pictures for the dewarping journeys: the fixture fisheye frame, the reference render of a view (the geometry's numpy implementation) and an export-style render
of it (FFmpeg's remap with the maps the exporter writes). Usage: ``frame OUT.png`` | ``reference OUT.png MODE PAN TILT FOV`` | ``export OUT.png MODE PAN TILT FOV``."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from app import dewarp
from tests.fixtures import fisheye_scene


def write_png(path: Path, picture: np.ndarray) -> None:
    height, width = picture.shape[:2]
    subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-i", "-", "-y", str(path)], input=picture.tobytes(), check=True)


def main(argv: list[str]) -> None:
    command, out = argv[0], Path(argv[1])
    frame = fisheye_scene.fisheye_frame()
    if command == "frame":
        write_png(out, frame)
        return
    mode, pan, tilt, fov = argv[2], float(argv[3]), float(argv[4]), float(argv[5])
    view = dewarp.View(mode, pan, tilt, fov)
    lens = dewarp.Lens()
    if command == "reference":
        write_png(out, dewarp.render(frame, lens, view))
        return
    xmap, ymap, size = dewarp.remap_maps(lens, view, frame.shape[1], frame.shape[0])
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_png(root / "f.png", frame)
        dewarp.write_pgm(root / "x.pgm", xmap)
        dewarp.write_pgm(root / "y.pgm", ymap)
        subprocess.run(["ffmpeg", "-v", "error", "-loop", "1", "-i", str(root / "f.png"), "-vf", dewarp.graph(root / "x.pgm", root / "y.pgm", size), "-frames:v", "1", "-y", str(out)], check=True)


if __name__ == "__main__":
    main(sys.argv[1:])
