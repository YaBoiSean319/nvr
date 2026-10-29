"""Capacity planner (#149): what a set of cameras needs in disk, write speed, network and processing.

This is an **estimate**, not a certification of capacity. Every figure is worked out from what is entered, from the bitrates this installation has measured when the
plan starts from its own cameras, and otherwise from the stated defaults below, which are listed with every result. The arithmetic is plain so it can be checked by hand:

- **Bitrate** of a camera: the one entered or measured, else a default for its resolution, scaled by frame rate over 15 and by 0.6 for H.265.
- **Duty**: the share of the day a camera records: 1 for continuous recording, ``motion_share`` for motion recording (or the share measured here), 0 for disabled.
- **Disk** = bitrate / 8 x 86 400 s x duty x retention days x cameras, then x ``overhead`` (container and filesystem), and x ``headroom`` for the recommended size.
- **Write speed**: average = sum of bitrate x duty; peak = sum of bitrate (every camera recording at once).
- **Network in**: every camera stays connected whether or not it is recording, so the sum of bitrates. **Network out**: viewers x tiles each x average bitrate x
  ``live_share`` (Live grids use the low-quality stream).
- **Processing**: recording and live viewing copy the stream (cores per Mbps); H.265 shown to a browser that cannot play it is converted (cores per converted stream, scaled by
  its size); AI analysis costs a decoded frame and an inference per analysed frame, on the processor or, where there is one, an accelerator.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

DISCLAIMER = ("This is an estimate, not a certification of capacity. Real footage varies with the scene, the camera's settings and the hardware; measure with a few cameras "
              "before buying for many, and keep headroom.")
SECONDS_PER_DAY = 86_400
REFERENCE_FPS = 15
WINDOW = timedelta(hours=48)
MIN_MEASURED_SECONDS = 3_600  # Under an hour of footage in the window is too little to call a bitrate measured
# Default bitrate (kbps, H.264, 15 frames a second) by picture size: the upper limit in megapixels, then the bitrate.
BITRATE_CLASSES = ((0.35, 512), (1.0, 1024), (2.2, 2048), (4.2, 3072), (5.5, 4096), (8.5, 6144), (float("inf"), 8192))
H265_FACTOR = 0.6
DEFAULT_PICTURE = (1920, 1080)
DEFAULT_FPS = 15
DEFAULT_CODEC = "h264"


@dataclass(frozen=True)
class Assumption:
    key: str
    label: str
    value: float
    unit: str
    low: float
    high: float


ASSUMPTIONS = (
    Assumption("motion_share", "Share of the day a motion-recorded camera records", 0.25, "fraction", 0.0, 1.0),
    Assumption("overhead", "Container and filesystem overhead on footage", 1.05, "x", 1.0, 2.0),
    Assumption("headroom", "Headroom added to the recommended disk size", 1.2, "x", 1.0, 3.0),
    Assumption("live_share", "Live-grid stream as a share of the camera's main bitrate", 0.25, "fraction", 0.05, 1.0),
    Assumption("record_cores_per_mbps", "Processor cores to record 1 Mbps (stream copy)", 0.01, "cores per Mbps", 0.0, 1.0),
    Assumption("live_cores_per_mbps", "Processor cores to send 1 Mbps to browsers (stream copy)", 0.005, "cores per Mbps", 0.0, 1.0),
    Assumption("hevc_transcode_share", "Share of viewed H.265 streams converted for the browser", 0.5, "fraction", 0.0, 1.0),
    Assumption("transcode_cores_1080p15", "Processor cores to convert one 1080p 15 fps H.265 stream", 0.5, "cores", 0.05, 8.0),
    Assumption("analysis_fps", "Frames analysed per second by AI, per AI camera", 0.5, "frames per second", 0.1, 5.0),
    Assumption("ai_frame_megapixels", "Size of the frame AI analyses (the low-quality stream)", 0.3, "megapixels", 0.05, 8.0),
    Assumption("decode_ms_per_megapixel", "Time to decode one megapixel of a frame", 6.0, "ms", 0.5, 100.0),
    Assumption("inference_ms_cpu", "Time for one AI inference on the processor", 40.0, "ms", 1.0, 1000.0),
    Assumption("inference_ms_accelerator", "Time for one AI inference on an accelerator", 8.0, "ms", 0.5, 1000.0),
)
ASSUMPTION_BY_KEY = {item.key: item for item in ASSUMPTIONS}
DEFAULTS = {item.key: item.value for item in ASSUMPTIONS}


@dataclass
class Row:
    """One line of the plan: ``count`` cameras alike in every way."""
    name: str
    count: int = 1
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    fps: float | None = None
    bitrate_kbps: float | None = None
    bitrate_source: str = "entered"  # entered, measured (when the plan came from this installation's cameras)
    recording: str = "continuous"  # continuous, motion or disabled
    recorded_share: float | None = None  # The share of time footage is recorded, when measured here (motion recording)
    retention_days: int = 30
    ai: bool = False
    analysis_fps: float | None = None
    assumed: list[str] = field(default_factory=list)


def default_bitrate(width: int, height: int, codec: str, fps: float) -> float:
    megapixels = width * height / 1_000_000
    base = next(kbps for limit, kbps in BITRATE_CLASSES if megapixels <= limit)
    return base * (fps / REFERENCE_FPS) * (H265_FACTOR if codec == "h265" else 1.0)


def _n(value: float) -> float:
    return round(value, 4)


def estimate(rows: list[Row], viewers: int, tiles_per_viewer: int, overrides: dict[str, float] | None = None) -> dict[str, Any]:
    """The estimate for a plan: totals, each line, and the assumptions every figure rests on."""
    values = {**DEFAULTS, **{key: float(value) for key, value in (overrides or {}).items() if key in ASSUMPTION_BY_KEY}}
    entered_overrides = {key for key in (overrides or {}) if key in ASSUMPTION_BY_KEY and float(overrides[key]) != DEFAULTS[key]}
    used: set[str] = set()
    lines: list[dict[str, Any]] = []
    cameras = sum(row.count for row in rows)
    disk = ingest_kbps = write_avg_kbps = write_peak_kbps = 0.0
    ai_cameras = 0
    ai_frames_per_second = 0.0
    hevc_cameras = 0
    hevc_pixels_fps = 0.0
    for row in rows:
        assumed: list[str] = list(row.assumed)
        width, height = (row.width, row.height) if row.width and row.height else DEFAULT_PICTURE
        if not (row.width and row.height):
            assumed.append("picture size (1920 x 1080)")
        codec = row.codec or DEFAULT_CODEC
        if not row.codec:
            assumed.append("codec (H.264)")
        fps = row.fps or DEFAULT_FPS
        if not row.fps:
            assumed.append("frame rate (15)")
        if row.bitrate_kbps:
            kbps, source = float(row.bitrate_kbps), row.bitrate_source
        else:
            kbps, source = default_bitrate(width, height, codec, fps), "assumed"
            assumed.append(f"bitrate ({kbps:.0f} kbps for this picture size and frame rate)")
        if row.recording == "continuous":
            duty = 1.0
        elif row.recording == "disabled":
            duty = 0.0
        elif row.recorded_share is not None:
            duty = row.recorded_share
        else:
            duty = values["motion_share"]
            used.add("motion_share")
            assumed.append(f"share of the day recorded ({duty:.0%}, motion recording)")
        bytes_per_day = kbps * 1000 / 8 * SECONDS_PER_DAY * duty
        line_disk = bytes_per_day * row.retention_days * row.count
        disk += line_disk
        ingest_kbps += kbps * row.count
        write_avg_kbps += kbps * duty * row.count
        write_peak_kbps += kbps * row.count if row.recording != "disabled" else 0.0
        if row.ai:
            ai_cameras += row.count
            ai_frames_per_second += (row.analysis_fps or values["analysis_fps"]) * row.count
            if not row.analysis_fps:
                used.add("analysis_fps")
        if codec == "h265":
            hevc_cameras += row.count
            hevc_pixels_fps += width * height * fps * row.count
        lines.append({
            "name": row.name, "count": row.count, "width": width, "height": height, "codec": codec, "fps": _n(fps), "recording": row.recording, "retention_days": row.retention_days,
            "ai": row.ai, "bitrate_kbps": _n(kbps), "bitrate_source": source, "duty": _n(duty), "bytes_per_day": round(bytes_per_day), "footage_bytes": round(line_disk),
            "assumed": assumed,
        })
    average_kbps = ingest_kbps / cameras if cameras else 0.0
    live_streams = viewers * tiles_per_viewer
    egress_kbps = live_streams * average_kbps * values["live_share"]
    recording_cores = ingest_kbps / 1000 * values["record_cores_per_mbps"]
    live_cores = egress_kbps / 1000 * values["live_cores_per_mbps"]
    hevc_fraction = hevc_cameras / cameras if cameras else 0.0
    converted = live_streams * hevc_fraction * values["hevc_transcode_share"]
    average_hevc_work = hevc_pixels_fps / hevc_cameras / (DEFAULT_PICTURE[0] * DEFAULT_PICTURE[1] * REFERENCE_FPS) if hevc_cameras else 0.0
    transcode_cores = converted * average_hevc_work * values["transcode_cores_1080p15"]
    decode_s = values["decode_ms_per_megapixel"] * values["ai_frame_megapixels"] / 1000
    ai_cpu = ai_frames_per_second * (decode_s + values["inference_ms_cpu"] / 1000)
    ai_accelerated = ai_frames_per_second * (decode_s + values["inference_ms_accelerator"] / 1000)
    for key in ("overhead", "headroom", "live_share", "record_cores_per_mbps", "live_cores_per_mbps"):
        used.add(key)
    if hevc_cameras:
        used.update({"hevc_transcode_share", "transcode_cores_1080p15"})
    if ai_cameras:
        used.update({"ai_frame_megapixels", "decode_ms_per_megapixel", "inference_ms_cpu", "inference_ms_accelerator"})

    def metric(label: str, value: float, unit: str, basis: str, *assumes: str) -> dict[str, Any]:
        return {"label": label, "value": round(value) if unit == "bytes" else _n(value), "unit": unit, "basis": basis, "assumes": [key for key in assumes if key in values]}

    footage = disk
    stored = footage * values["overhead"]
    metrics = {
        "disk_footage": metric("Footage kept", footage, "bytes", "bitrate / 8 x 86 400 s x share of the day recorded x retention days, summed over cameras"),
        "disk_needed": metric("Disk needed", stored, "bytes", "footage x overhead", "overhead"),
        "disk_recommended": metric("Disk recommended", stored * values["headroom"], "bytes", "disk needed x headroom", "overhead", "headroom"),
        "write_average": metric("Write speed, average", write_avg_kbps / 8 / 1000, "MB/s", "sum of bitrate x share of the day recorded, over 8", "motion_share"),
        "write_peak": metric("Write speed, peak", write_peak_kbps / 8 / 1000, "MB/s", "every recording camera writing at once"),
        "network_in": metric("Network in (cameras to the NVR)", ingest_kbps / 1000, "Mbps", "sum of every camera's bitrate: each stays connected whether or not it is recording"),
        "network_out": metric("Network out (viewers)", egress_kbps / 1000, "Mbps", "viewers x tiles each x average bitrate x live share", "live_share"),
        "cores_streams": metric("Processor, recording and live copying", recording_cores + live_cores, "cores", "cores per Mbps x network in, and x network out",
                                "record_cores_per_mbps", "live_cores_per_mbps"),
        "cores_conversion": metric("Processor, H.265 conversion for browsers", transcode_cores, "cores", "viewed H.265 streams converted x cores per 1080p 15 fps stream, scaled by their size",
                                   "hevc_transcode_share", "transcode_cores_1080p15"),
        "cores_ai_processor": metric("Processor, AI analysis (no accelerator)", ai_cpu, "cores", "analysed frames per second x (decode + inference on the processor)",
                                     "analysis_fps", "ai_frame_megapixels", "decode_ms_per_megapixel", "inference_ms_cpu"),
        "cores_ai_accelerated": metric("Processor, AI analysis (with an accelerator)", ai_accelerated, "cores", "as above with the inference on the accelerator (it needs the accelerator's own capacity too)",
                                       "analysis_fps", "ai_frame_megapixels", "decode_ms_per_megapixel", "inference_ms_accelerator"),
    }
    metrics["cores_total_processor"] = metric("Processor total, no accelerator", recording_cores + live_cores + transcode_cores + ai_cpu, "cores", "the four processor lines above, AI on the processor",
                                              *sorted({key for item in metrics.values() for key in item["assumes"]}))
    metrics["cores_total_accelerated"] = metric("Processor total, with an accelerator", recording_cores + live_cores + transcode_cores + ai_accelerated, "cores", "as above, AI inference on the accelerator",
                                                *sorted({key for item in metrics.values() for key in item["assumes"]}))
    return {
        "cameras": cameras, "viewers": viewers, "tiles_per_viewer": tiles_per_viewer, "ai_cameras": ai_cameras, "lines": lines, "metrics": metrics,
        "assumptions": [{"key": item.key, "label": item.label, "value": values[item.key], "unit": item.unit, "source": "entered" if item.key in entered_overrides else "default",
                         "used": item.key in used} for item in ASSUMPTIONS],
        "defaults": {"bitrate_kbps_h264_15fps": [{"up_to_megapixels": None if limit == float("inf") else limit, "kbps": kbps} for limit, kbps in BITRATE_CLASSES], "h265_factor": H265_FACTOR},
        "disclaimer": DISCLAIMER,
    }


def _codec(value: str | None) -> str | None:
    text = (value or "").lower()
    if text in {"h265", "hevc", "h.265"}:
        return "h265"
    return "h264" if text in {"h264", "avc", "avc1", "h.264"} else None


def inventory(db: sqlite3.Connection, now: datetime | None = None) -> list[dict[str, Any]]:
    """The installation's own cameras as plan lines: picture and codec as probed, retention and recording mode as set, and the bitrate and share of time recorded as
    measured over the last 48 hours where there is enough footage. What could not be known is left empty, and the estimate labels it assumed."""
    from .detection import effective_camera_config
    from .retention import effective_retention
    now = now or datetime.now(timezone.utc)
    start = (now - WINDOW).isoformat()
    measured = {row["camera_id"]: row for row in db.execute(
        "SELECT camera_id,SUM(size_bytes) AS bytes,SUM((julianday(ends_at)-julianday(starts_at))*86400) AS seconds FROM recording_segments "
        "WHERE unavailable_at IS NULL AND starts_at>=? GROUP BY camera_id", (start,))}
    rows = []
    for camera in db.execute("SELECT id,name,video_codec,video_width,video_height,recording_mode FROM cameras WHERE archived_at IS NULL ORDER BY name,id").fetchall():
        found = measured.get(camera["id"])
        seconds = float(found["seconds"] or 0) if found else 0.0
        kbps = round(found["bytes"] * 8 / seconds / 1000, 1) if found and seconds >= MIN_MEASURED_SECONDS - 1 else None  # (a second of slack: the durations come out of date arithmetic)
        share = min(1.0, seconds / WINDOW.total_seconds()) if kbps is not None else None
        config = effective_camera_config(camera["id"])
        retention, _ = effective_retention(db, camera["id"])
        rows.append({
            "name": camera["name"], "count": 1, "width": camera["video_width"], "height": camera["video_height"], "codec": _codec(camera["video_codec"]), "fps": None,
            "bitrate_kbps": kbps, "bitrate_source": "measured" if kbps is not None else "assumed", "recording": camera["recording_mode"],
            "recorded_share": round(share, 4) if share is not None and camera["recording_mode"] == "motion" else None, "retention_days": retention,
            "ai": bool(config and config.get("enabled")), "analysis_fps": float(config["analysis_fps"]) if config and config.get("enabled") and config.get("analysis_fps") else None,
        })
    return rows
