"""The real pose model for the fall-detection tests (#230): downloaded once into a cache folder, checked against the pinned checksums and unpacked.

``folder()`` returns the folder, or None when it cannot be had (no network). With ``NVR_REQUIRE_POSE_MODELS=1`` (CI) a failure raises instead, so the real-model tests can never
be skipped there without anyone noticing.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app import pose_models

_CACHE = Path(os.environ.get("NVR_TEST_POSE_MODEL_CACHE", Path(tempfile.gettempdir()) / "nvr-pose-model-cache"))
_state: dict[str, Path | None] = {}


def required() -> bool:
    return os.environ.get("NVR_REQUIRE_POSE_MODELS") == "1"


def folder() -> Path | None:
    if "folder" in _state:
        return _state["folder"]
    import urllib.request
    _CACHE.mkdir(parents=True, exist_ok=True)
    try:
        if not pose_models.installed(_CACHE):
            if not pose_models.file_ok(pose_models.BUNDLE, _CACHE):
                with urllib.request.urlopen(pose_models.BUNDLE.url, timeout=60) as response:  # noqa: S310 - the URL is pinned in the registry, https
                    data = response.read()
                temporary = _CACHE / (pose_models.BUNDLE.name + ".part")
                temporary.write_bytes(data)
                temporary.replace(_CACHE / pose_models.BUNDLE.name)
            if not pose_models.file_ok(pose_models.BUNDLE, _CACHE):
                raise OSError("The pose model bundle does not match its pinned checksum")
            pose_models.extract(_CACHE / pose_models.BUNDLE.name, _CACHE)
        _state["folder"] = _CACHE if pose_models.installed(_CACHE) else None
    except OSError:
        if required():
            raise
        _state["folder"] = None
    return _state["folder"]
