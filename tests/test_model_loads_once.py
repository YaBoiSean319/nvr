"""Every camera that starts at once gets the one shared copy of each AI model (#217).

When the AI worker starts (or restarts after being killed), every camera's analysis starts within moments, and each asks for the vehicle
attribute, person re-identification, pose, plate and face models. Each model was loaded in a thread and remembered once loaded, so every
camera that asked before the first load finished saw none and loaded its own: as many copies as cameras, at once, and each camera kept its
own copy for as long as it ran. On a large site that is a burst of memory at the moment the worker can least afford it, and a lasting
multiple of each model's size after it.
"""
from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from app.detection import DetectionManager

CAMERAS = 12


class SharedLoadTests(unittest.TestCase):
    def loaded_by_cameras_at_once(self, ask, target: str):
        loads: list[object] = []
        guard = threading.Lock()

        def load(*args, **kwargs):
            time.sleep(0.05)  # Reading and compiling a model takes a while
            model = object()
            with guard:
                loads.append(model)
            return model

        async def run():
            manager = DetectionManager()
            return await asyncio.gather(*(ask(manager) for _ in range(CAMERAS)))

        with patch(target, load):
            given = asyncio.run(run())
        self.assertEqual(len(loads), 1, f"{len(loads)} copies were loaded for {CAMERAS} cameras")
        self.assertTrue(all(model is loads[0] for model in given), "a camera was given a copy other than the shared one")

    def test_the_vehicle_attribute_model(self):
        self.loaded_by_cameras_at_once(lambda manager: manager._attribute_model("CPU"), "app.attributes.load_attribute_model")

    def test_the_person_reidentification_model(self):
        self.loaded_by_cameras_at_once(lambda manager: manager._reid_model("CPU"), "app.similar.load_reid_model")

    def test_the_pose_model(self):
        self.loaded_by_cameras_at_once(lambda manager: manager._pose_reader("CPU"), "app.pose_models.load_reader")

    def test_the_plate_models(self):
        self.loaded_by_cameras_at_once(lambda manager: manager._plate_reader("CPU", "worldwide"), "app.plates.load_reader")

    def test_the_face_models(self):
        self.loaded_by_cameras_at_once(lambda manager: manager._face_reader("CPU"), "app.face_models.load_reader")

    def test_a_model_that_is_not_installed_is_looked_for_again_later(self):
        # Pose, plate and face models can be installed while the worker runs: a failed load is not remembered, so the next camera tries again.
        answers = iter([None, "reader"])

        async def run():
            manager = DetectionManager()
            return await manager._pose_reader("CPU"), await manager._pose_reader("CPU")

        with patch("app.pose_models.load_reader", lambda provider: next(answers)):
            self.assertEqual(asyncio.run(run()), (None, "reader"))


class FaceSearchReaderTests(unittest.TestCase):
    def test_two_face_searches_at_once_load_one_reader(self):
        # The web server's face search loads its reader the first time it is needed, from a request's thread.
        from concurrent.futures import ThreadPoolExecutor

        from app import face_models, faces

        loads: list[object] = []

        def load(*args, **kwargs):
            time.sleep(0.05)
            loads.append(object())
            return loads[-1]

        with patch.object(faces, "_reader", None), patch.object(faces, "_reader_failed", False), \
                patch.object(face_models, "installed", lambda: True), patch.object(face_models, "load_reader", load):
            with ThreadPoolExecutor(max_workers=CAMERAS) as pool:
                given = list(pool.map(lambda _: faces.reader(), range(CAMERAS)))
        self.assertEqual(len(loads), 1)
        self.assertTrue(all(reader is loads[0] for reader in given))


if __name__ == "__main__":
    unittest.main()
