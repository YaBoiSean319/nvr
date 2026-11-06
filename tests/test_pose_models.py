"""The pose model for fall detection (#230): the registry and its pins, the crop and decoding, and the real model's contract."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app import pose_models
from tests.fixtures import real_pose_models

ROOT = Path(__file__).resolve().parent.parent


class RegistryTests(unittest.TestCase):
    def test_the_dockerfile_installs_exactly_the_pinned_bundle(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn(pose_models.BUNDLE.url, dockerfile)
        self.assertIn(f"{pose_models.BUNDLE.sha256}  /opt/nvr/models/pose/{pose_models.BUNDLE.name}", dockerfile)
        self.assertIn(pose_models.LANDMARKS.sha256, dockerfile)  # The unpacked model is checked as well

    def test_nothing_is_installed_until_the_pinned_model_is_there(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(pose_models.installed(Path(folder)))
            (Path(folder) / pose_models.LANDMARKS.name).write_bytes(b"not the model")
            self.assertFalse(pose_models.installed(Path(folder)))  # A file that is not the pinned one does not count

    def test_the_licence_is_stated_and_no_licensed_model_is_in_the_registry(self):
        self.assertIn("Apache-2.0", pose_models.status()["licence"])
        for model in pose_models.SHIPPED:
            self.assertTrue(model.url.startswith("https://storage.googleapis.com/mediapipe-models/"), model.url)


class CropAndDecodeTests(unittest.TestCase):
    def test_the_crop_is_square_around_the_person_with_a_margin(self):
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[300:500, 600:700] = 255
        crop, left, top, side = pose_models.square_crop(frame, {"x": 600 / 1280, "y": 300 / 720, "width": 100 / 1280, "height": 200 / 720})
        self.assertEqual(crop.shape[0], crop.shape[1])
        self.assertAlmostEqual(side, 200 * pose_models.CROP_MARGIN, delta=1)
        self.assertAlmostEqual(left + side / 2, 650, delta=1)  # Centred on the person
        self.assertAlmostEqual(top + side / 2, 400, delta=1)
        self.assertGreater(crop.mean(), 0)  # The person is in it

    def test_a_person_at_the_edge_is_padded_not_cut_off_or_shifted(self):
        frame = np.full((100, 100, 3), 200, dtype=np.uint8)
        crop, left, top, side = pose_models.square_crop(frame, {"x": 0.0, "y": 0.0, "width": 0.4, "height": 0.4})
        self.assertLess(left, 0)  # The square starts outside the picture
        self.assertEqual(int(crop[0, 0, 0]), 0)  # ...and is black there
        self.assertEqual(int(crop[-1, -1, 0]), 200)  # ...and the picture where it has one

    def test_the_points_map_from_the_crop_back_to_the_frame(self):
        landmarks = np.zeros((39, 5), dtype=np.float32)
        landmarks[:, 3] = 5.0  # Visible
        landmarks[11] = (128, 64, 0, 5.0, 0)  # Left shoulder: the middle of the crop, a quarter down
        found = pose_models.decode(landmarks.reshape(-1), 0.9, rows=720, columns=1280, left=500.0, top=100.0, side=400.0)
        x, y, visibility = found["left_shoulder"]
        self.assertAlmostEqual(x, (500 + 128 / 256 * 400) / 1280, places=4)
        self.assertAlmostEqual(y, (100 + 64 / 256 * 400) / 720, places=4)
        self.assertGreater(visibility, 0.99)
        self.assertEqual(set(found), set(pose_models.POINTS))

    def test_the_arms_are_read_for_drawing_but_not_for_the_fall_decision(self):
        landmarks = np.zeros((39, 5), dtype=np.float32)
        landmarks[:, 3] = 5.0
        found = pose_models.decode(landmarks.reshape(-1), 0.9, rows=720, columns=1280, left=0.0, top=0.0, side=256.0, names=pose_models.DRAWN_POINTS)
        self.assertEqual(set(found), set(pose_models.DRAWN_POINTS))
        self.assertTrue({"left_elbow", "right_elbow", "left_wrist", "right_wrist"} <= set(found))
        self.assertEqual(set(pose_models.POINTS), set(pose_models.DRAWN_POINTS) - {"left_elbow", "right_elbow", "left_wrist", "right_wrist"})  # The decision keeps its nine

    def test_a_crop_the_model_doubts_holds_a_person_gives_nothing(self):
        self.assertIsNone(pose_models.decode(np.zeros(195), 0.1, 720, 1280, 0, 0, 256))


@unittest.skipUnless(real_pose_models.folder() is not None or real_pose_models.required(), "needs the pose model (network)")
class RealModelTests(unittest.TestCase):
    def test_the_real_model_loads_has_the_contract_the_reader_expects_and_finds_nobody_in_an_empty_room(self):
        folder = real_pose_models.folder()
        reader = pose_models.PoseReader(directory=folder)
        self.assertEqual(list(reader.model.input(0).shape), [1, 256, 256, 3])
        self.assertEqual(list(reader._landmarks.shape), [1, 195])
        frame = np.full((720, 1280, 3), 90, dtype=np.uint8)  # An empty, flat picture
        self.assertIsNone(reader.pose(frame, {"x": 0.4, "y": 0.2, "width": 0.2, "height": 0.6}))


if __name__ == "__main__":
    unittest.main()
