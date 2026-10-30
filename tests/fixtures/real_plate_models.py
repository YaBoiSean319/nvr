"""The real shipped plate models for the region tests (#210): downloaded once into a cache folder and checked against the pinned checksums.

``folder()`` returns the folder, or None when they cannot be had (no network). With ``NVR_REQUIRE_PLATE_MODELS=1`` (CI) a failure raises instead, so the real-model tests
can never be skipped there without anyone noticing.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from app import plate_models

_CACHE = Path(os.environ.get("NVR_TEST_PLATE_MODEL_CACHE", Path(tempfile.gettempdir()) / "nvr-plate-model-cache"))
_state: dict[str, Path | None | bool] = {}


def required() -> bool:
    return os.environ.get("NVR_REQUIRE_PLATE_MODELS") == "1"


def folder() -> Path | None:
    if "folder" in _state:
        return _state["folder"]  # type: ignore[return-value]
    import urllib.request
    _CACHE.mkdir(parents=True, exist_ok=True)
    try:
        for model in plate_models.SHIPPED:
            target = _CACHE / model.name
            if plate_models.file_ok(model, _CACHE):
                continue
            with urllib.request.urlopen(model.url, timeout=60) as response:  # noqa: S310 - the URL is pinned in the registry, https
                data = response.read()
            temporary = target.with_suffix(target.suffix + ".part")
            temporary.write_bytes(data)
            temporary.replace(target)
        _state["folder"] = _CACHE
    except OSError:
        if required():
            raise
        _state["folder"] = None
    return _state["folder"]  # type: ignore[return-value]


FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf")


def scene(text: str = "AB12CDE") -> np.ndarray | None:
    """A 1280 x 720 BGR picture of a dark car body with a plate drawn on it (ffmpeg), or None when ffmpeg or a font is missing. Drawn, not photographed."""
    font = next((path for path in FONTS if Path(path).is_file()), None)
    if font is None or subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0:
        return None
    graph = (f"drawbox=x=300:y=250:w=680:h=380:c=0x3a4048:t=fill,drawbox=x=380:y=520:w=520:h=64:c=0x111111:t=fill,drawbox=x=455:y=455:w=370:h=84:c=0xf2f2f0:t=fill,"
             f"drawbox=x=455:y=455:w=370:h=84:c=0x202020:t=4,drawtext=text='{text}':fontfile={font}:fontsize=66:x=474:y=468:fontcolor=0x101010,drawbox=x=462:y=462:w=8:h=70:c=0x1a3a9a:t=fill")
    raw = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "gradients=s=1280x720:c0=0x1d2733:c1=0x586674:x0=0:y0=0:x1=1280:y1=720:d=1", "-vf", graph, "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True)
    if raw.returncode != 0 or len(raw.stdout) != 1280 * 720 * 3:
        return None
    return np.frombuffer(raw.stdout, dtype=np.uint8).reshape(720, 1280, 3).copy()
