"""The person and vehicle detector runs one inference at a time (#436).

An OpenVINO compiled model has one inference request, which refuses a second call while the first is under way ("Infer Request is busy").
Cameras take turns at the detector through the AI worker's inference slot, but a camera stopped in the middle of a frame gives the slot
back while its thread is still inside the model: the next camera's frame then reached the model while it was busy, and was lost.
"""
from __future__ import annotations

import asyncio
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from app import detection

INFERENCE_SECONDS = 0.3


class Port:
    def __init__(self, name: str, shape=None) -> None:
        self.name, self.shape = name, shape

    def get_names(self) -> set[str]:
        return {self.name}


class OneRequestModel:
    """Like a compiled detector: one inference request, which refuses to run twice at once; one person found in every frame."""

    def __init__(self, seconds: float) -> None:
        self.seconds, self.busy, self.calls = seconds, threading.Lock(), 0
        self.inputs = [Port("image", (1, 3, detection.MODEL_HEIGHT, detection.MODEL_WIDTH))]
        self.boxes, self.labels = Port("boxes"), Port("labels")
        self.outputs = [self.boxes, self.labels]

    def input(self, index: int) -> Port:
        return self.inputs[index]

    def __call__(self, tensors):
        if not self.busy.acquire(blocking=False):
            raise RuntimeError("Infer Request is busy")
        try:
            time.sleep(self.seconds)
            self.calls += 1
            return {self.boxes: np.array([[[10.0, 20.0, 110.0, 220.0, 0.9]]], dtype=np.float32), self.labels: np.array([[1]], dtype=np.int64)}
        finally:
            self.busy.release()


class FakeCore:
    available_devices = ["CPU"]

    def __init__(self, model: OneRequestModel) -> None:
        self.model = model

    def read_model(self, path: str) -> str:
        return path

    def compile_model(self, model, provider, properties=None) -> OneRequestModel:
        return self.model


def detector(seconds: float) -> tuple[detection.OpenVINODetector, OneRequestModel]:
    model = OneRequestModel(seconds)
    return detection.OpenVINODetector(core=FakeCore(model)), model


FRAME = bytes(detection.MODEL_FRAME_BYTES)


class DetectorOneRequestTests(unittest.TestCase):
    def test_frames_from_several_threads_at_once_are_each_analysed(self):
        shared, model = detector(0.003)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: shared.infer(FRAME, 0.5), range(40)))
        self.assertEqual(model.calls, 40)
        self.assertTrue(all(result and result[0]["label"] == "person" for result in results))

    def test_a_camera_stopped_mid_frame_does_not_cost_the_next_camera_its_frame(self):
        shared, model = detector(INFERENCE_SECONDS)
        config = {"analysis_fps": 1.0, "confidence_threshold": 0.5, "effective_vehicle_confidence_threshold": 0.5}

        async def scenario():
            manager = detection.DetectionManager()
            stopped = asyncio.create_task(manager._detect("camera-a", config, shared, FRAME, 100))
            await asyncio.sleep(INFERENCE_SECONDS / 3)  # Camera A's frame is inside the model
            stopped.cancel()  # Camera A is stopped: its turn at the slot ends, though its thread is still running the model
            with self.assertRaises(asyncio.CancelledError):
                await stopped
            return await manager._detect("camera-b", config, shared, FRAME, 100)

        observations = asyncio.run(scenario())
        self.assertEqual([item["label"] for item in observations], ["person"])
        self.assertEqual(model.calls, 2)


if __name__ == "__main__":
    unittest.main()
