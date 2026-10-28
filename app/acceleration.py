"""Hardware video acceleration (#133): what this machine has, and the FFmpeg arguments to use it.

Transcoding (live H.265 to H.264, compatible playback copies), AI decoding and export rendering use a graphics chip when there is one: NVIDIA
(NVENC/NVDEC), Intel Quick Sync (QSV) or VAAPI. Each process checks once, the first time it needs to: it looks for the encoder in FFmpeg and for the device,
and tries a short real encode, so a card that is listed but does not work is not chosen. ``NVR_HWACCEL`` sets it: ``auto`` (the default), ``none`` for
software only, or one of ``nvenc``, ``qsv``, ``vaapi`` to use only that. ``NVR_VAAPI_DEVICE`` names the render device (default the first
``/dev/dri/renderD*``). If an accelerated encode fails when it is used, the work is done again in software and acceleration stays off until restart, with the reason in
the status. Without acceleration everything uses software (libx264), within the same slot limits.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

BACKENDS = ("nvenc", "qsv", "vaapi")  # In order of preference
LABELS = {"nvenc": "NVIDIA NVENC/NVDEC", "qsv": "Intel Quick Sync", "vaapi": "VAAPI"}
ENCODERS = {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "vaapi": "h264_vaapi"}
PROBE_SECONDS = 20

Runner = Callable[[list[str], int], tuple[int, str]]


@dataclass(frozen=True)
class Encoder:
    """How to encode H.264 with the chosen backend: arguments before the input, a filter to end the chain with, and the codec arguments."""
    name: str | None
    pre: tuple[str, ...]
    vf_tail: str
    codec: tuple[str, ...]

    def filters(self, chain: str | None) -> str | None:
        """A filter chain that ends with what this encoder needs (frames uploaded to the device, say), or None when there is nothing to filter."""
        parts = [part for part in (chain, self.vf_tail) if part]
        return ",".join(parts) if parts else None


def _run(args: list[str], timeout: int) -> tuple[int, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout


def _vaapi_device(environ: dict[str, str]) -> str:
    return environ.get("NVR_VAAPI_DEVICE", "").strip() or next(iter(sorted(glob.glob("/dev/dri/renderD*"))), "/dev/dri/renderD128")


def build(backend: str | None, *, crf: int = 23, speed: str = "veryfast", environ: dict[str, str] | None = None) -> Encoder:
    """The encoder for a backend (None: software). ``speed`` is an x264 preset name; it is mapped to each backend's own scale."""
    environ = os.environ if environ is None else environ  # type: ignore[assignment]
    fast = speed in {"ultrafast", "superfast", "veryfast", "zerolatency"}
    if backend == "nvenc":
        return Encoder("nvenc", (), "", ("-c:v", "h264_nvenc", "-preset", "p1" if fast else "p4", "-rc", "vbr", "-cq", str(crf), "-b:v", "0"))
    if backend == "qsv":
        return Encoder("qsv", (), "format=nv12", ("-c:v", "h264_qsv", "-global_quality", str(crf), "-preset", "veryfast" if fast else "medium"))
    if backend == "vaapi":
        return Encoder("vaapi", ("-vaapi_device", _vaapi_device(environ)), "format=nv12,hwupload", ("-c:v", "h264_vaapi", "-qp", str(crf)))  # type: ignore[arg-type]
    return Encoder(None, (), "", ("-c:v", "libx264", "-preset", speed, "-crf", str(crf)))


def _decode(backend: str | None, environ: dict[str, str]) -> list[str]:
    if backend == "nvenc":
        return ["-hwaccel", "cuda"]
    if backend == "qsv":
        return ["-hwaccel", "qsv"]
    if backend == "vaapi":
        return ["-hwaccel", "vaapi", "-hwaccel_device", _vaapi_device(environ)]
    return []


def _device_present(backend: str) -> bool:
    if backend == "nvenc":
        return any(os.path.exists(path) for path in ("/dev/nvidiactl", "/dev/nvidia0"))
    return bool(glob.glob("/dev/dri/renderD*"))


def detect(run: Runner = _run, *, environ: dict[str, str] | None = None, device_present: Callable[[str], bool] = _device_present,
           ffmpeg: str | None = None) -> dict[str, Any]:
    """Which acceleration works here. Never raises: whatever cannot be checked is reported as not available."""
    environ = dict(os.environ if environ is None else environ)
    setting = environ.get("NVR_HWACCEL", "auto").strip().lower() or "auto"
    note = None
    if setting not in {"auto", "none", *BACKENDS}:
        note, setting = f"NVR_HWACCEL={setting} is not one of auto, none, nvenc, qsv, vaapi; using auto.", "auto"
    report: dict[str, Any] = {"setting": setting, "selected": None, "software_encoder": "libx264", "ffmpeg": False, "backends": [], "note": note,
                              "disabled_reason": None, "checked_at": datetime.now(timezone.utc).isoformat()}
    executable = ffmpeg or shutil.which("ffmpeg")
    if not executable:
        report["note"] = "FFmpeg is not installed, so no video work can be done."
        return report
    report["ffmpeg"] = True
    if setting == "none":
        report["note"] = "Hardware acceleration is turned off (NVR_HWACCEL=none): software encoding is used."
        return report
    _, listing = run([executable, "-hide_banner", "-encoders"], 15)
    for backend in ([setting] if setting in BACKENDS else BACKENDS):
        entry: dict[str, Any] = {"name": backend, "label": LABELS[backend], "encoder": ENCODERS[backend] in listing, "device": None, "usable": False, "detail": ""}
        report["backends"].append(entry)
        if not entry["encoder"]:
            entry["detail"] = f"This FFmpeg has no {ENCODERS[backend]} encoder"
            continue
        entry["device"] = device_present(backend)
        if not entry["device"]:
            entry["detail"] = "No device found" + (" (no /dev/dri render device: pass one into the container)" if backend != "nvenc" else " (no NVIDIA device)")
            continue
        encoder = build(backend, crf=28, speed="veryfast", environ=environ)
        code, _ = run([executable, "-hide_banner", "-loglevel", "error", *encoder.pre, "-f", "lavfi", "-i", "testsrc=size=320x240:rate=5", "-t", "0.5",
                       *(["-vf", encoder.vf_tail] if encoder.vf_tail else []), *encoder.codec, "-f", "null", "-"], PROBE_SECONDS)
        entry["usable"] = code == 0
        entry["detail"] = "A test encode worked" if code == 0 else "A test encode failed: the driver or device is not usable from here"
        if entry["usable"] and report["selected"] is None:
            report["selected"] = backend
    if report["selected"] is None and report["note"] is None:
        report["note"] = "No hardware acceleration is available: software encoding is used."
    return report


_lock = threading.Lock()
_state: dict[str, Any] | None = None


def current() -> dict[str, Any]:
    """This process's view of the machine, found the first time it is asked for."""
    global _state
    with _lock:
        if _state is not None:
            return dict(_state)
    found = detect()  # Slow (it runs FFmpeg): never while holding the lock
    with _lock:
        if _state is None:
            _state = found
        return dict(_state)


def refresh() -> dict[str, Any]:
    """Check the machine again. A failure already seen in use is kept: it is not undone by a fresh look."""
    global _state
    found = detect()
    with _lock:
        if _state is not None and _state.get("disabled_reason"):
            found["selected"], found["disabled_reason"] = None, _state["disabled_reason"]
        _state = found
        return dict(found)


def selected() -> str | None:
    return current()["selected"]


def disable(reason: str) -> None:
    """Stop using acceleration in this process after it failed in use; the work is done in software."""
    with _lock:
        if _state is not None and _state["selected"] is not None:
            _state["disabled_reason"] = f"Turned off after a failure: {reason}"[:300]
            _state["selected"] = None


def encoder(*, crf: int = 23, speed: str = "veryfast") -> Encoder:
    return build(selected(), crf=crf, speed=speed)


def decode_args() -> list[str]:
    """Arguments (before ``-i``) that decode with the device; frames come back to system memory for the software filters."""
    return _decode(selected(), dict(os.environ))


def reset_for_tests() -> None:
    global _state
    with _lock:
        _state = None
