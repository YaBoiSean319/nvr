#!/usr/bin/env python3
"""Measure the bundled CPU detector on fixed-size synthetic frames."""
from __future__ import annotations

import json
import os
import platform
import statistics
import time

import numpy as np

from app.detection import MODEL_FRAME_BYTES, MODEL_HEIGHT, MODEL_ID, MODEL_VERSION, MODEL_WIDTH, OpenVINOCPUDetector


def main() -> None:
    detector = OpenVINOCPUDetector()
    frame = np.zeros((MODEL_HEIGHT, MODEL_WIDTH, 3), dtype=np.uint8)
    # Use a repeatable textured frame so measurements include the same conversion work as live frames.
    frame[::16, :, :] = 127
    raw = frame.tobytes()
    assert len(raw) == MODEL_FRAME_BYTES
    for _ in range(5):
        detector.infer(raw, 0.55)
    samples = []
    for _ in range(100):
        started = time.perf_counter()
        detector.infer(raw, 0.55)
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    p50 = statistics.median(samples)
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    try:
        import openvino
        runtime_version = openvino.__version__
    except Exception:
        runtime_version = "unknown"
    print(json.dumps({
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "runtime": runtime_version,
        "execution_device": "CPU",
        "cpu": platform.processor() or platform.machine(),
        "configured_threads": max(1, min(int(os.environ.get("NVR_AI_CPU_THREADS", "2")), 8)),
        "input": {"width": MODEL_WIDTH, "height": MODEL_HEIGHT, "format": "BGR"},
        "warmup_frames": 5,
        "measured_frames": len(samples),
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "model_only_fps_estimate": round(1000 / statistics.mean(samples), 2),
        "scope": "synthetic fixed-size frames; excludes decoding, network, recording, storage, and browser workloads",
    }, indent=2))


if __name__ == "__main__":
    main()
