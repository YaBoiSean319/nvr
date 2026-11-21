"""The face models (#253): the registry, the decoding of YuNet's output, alignment, and comparing faces. The real models are exercised by hand (docs/face-watch.md); these prove the code."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import numpy as np

from app import face_models as fm

ROOT = Path(__file__).resolve().parent.parent


class RegistryTests(unittest.TestCase):
    def test_every_model_is_pinned_by_url_and_checksum_and_the_dockerfile_installs_exactly_those(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        for model in fm.SHIPPED:
            self.assertTrue(model.url.startswith("https://media.githubusercontent.com/media/opencv/opencv_zoo/"), model.url)
            self.assertRegex(model.sha256, r"^[0-9a-f]{64}$")
            self.assertGreater(model.size_bytes, 0)
            self.assertIn(model.url, dockerfile)
            self.assertIn(f"{model.sha256}  /opt/nvr/models/faces/{model.name}", dockerfile)

    def test_the_licences_are_stated_and_no_non_commercial_model_is_used(self):
        self.assertIn("MIT", fm.__doc__)
        self.assertIn("Apache-2.0", fm.__doc__)
        self.assertIn("non-commercial", fm.__doc__)  # Says why InsightFace's weights were not used
        for model in fm.SHIPPED:
            self.assertNotIn("insightface", model.url.lower())

    def test_a_missing_or_altered_file_is_not_installed(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(fm.installed(Path(folder)))
            for model in fm.SHIPPED:
                (Path(folder) / model.name).write_bytes(b"not the model")
            self.assertFalse(fm.installed(Path(folder)))
        self.assertFalse(fm.status()["available"])  # Nothing is installed in the test environment


class GeometryTests(unittest.TestCase):
    def test_letterbox_keeps_the_shape_and_puts_the_picture_at_the_top_left(self):
        frame = np.full((300, 600, 3), 200, dtype=np.uint8)
        tensor, scale = fm.letterbox(frame)
        self.assertEqual((tensor.shape, round(scale, 4)), ((1, 3, 640, 640), round(640 / 600, 4)))
        self.assertGreater(tensor[0, 0, 100, 100], 0)  # Inside the picture (320 rows of the 640)
        self.assertEqual(float(tensor[0, 0, 500, 100]), 0.0)  # Below it: black

    def test_nms_keeps_the_best_of_boxes_that_overlap(self):
        boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], dtype=float)
        self.assertEqual(fm.nms(boxes, np.array([0.8, 0.9, 0.7])), [1, 2])

    def test_a_similarity_transform_is_recovered_and_applied_to_a_picture(self):
        angle, scale, shift = 0.3, 1.7, np.array([12.0, -5.0])
        rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
        source = np.array([[10, 20], [60, 25], [35, 50], [18, 80], [55, 82]], dtype=float)
        target = (scale * (rotation @ source.T)).T + shift
        matrix = fm.similarity_transform(source, target)
        np.testing.assert_allclose(matrix[:, :2], scale * rotation, atol=1e-6)
        np.testing.assert_allclose(matrix[:, 2], shift, atol=1e-6)
        picture = np.random.default_rng(3).integers(0, 255, (112, 112, 3), dtype=np.uint8)
        np.testing.assert_allclose(fm.warp(picture, np.array([[1.0, 0, 0], [0, 1, 0]])), picture.astype(np.float32), atol=1e-4)  # The identity changes nothing

    def test_alignment_puts_the_landmarks_on_the_template(self):
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        landmarks = np.array([[150, 100], [250, 100], [200, 150], [160, 200], [240, 200]], dtype=float)  # A face 100 px wide
        matrix = fm.similarity_transform(landmarks, fm.TEMPLATE)
        moved = (matrix[:, :2] @ landmarks.T).T + matrix[:, 2]
        self.assertLess(float(np.abs(moved - fm.TEMPLATE).max()), 6.0)  # Near it: a real face's points are not exactly the template's shape
        self.assertEqual(fm.align(frame, landmarks).shape, (112, 112, 3))


def synthetic_outputs(cell_row: int, cell_col: int, stride: int, score: float, size: int = 640) -> dict[str, np.ndarray]:
    """YuNet-shaped outputs with one strong cell: a box of 3 x 4 strides centred a quarter of a stride right of and below the cell's corner, and landmarks set around it."""
    out: dict[str, np.ndarray] = {}
    for s in fm.STRIDES:
        cells = (size // s) ** 2
        out[f"cls_{s}"], out[f"obj_{s}"] = np.zeros((1, cells, 1), np.float32), np.zeros((1, cells, 1), np.float32)
        out[f"bbox_{s}"], out[f"kps_{s}"] = np.zeros((1, cells, 4), np.float32), np.zeros((1, cells, 10), np.float32)
    index = cell_row * (size // stride) + cell_col
    out[f"cls_{stride}"][0, index, 0] = out[f"obj_{stride}"][0, index, 0] = score
    out[f"bbox_{stride}"][0, index] = [0.25, 0.25, np.log(3), np.log(4)]
    out[f"kps_{stride}"][0, index] = [-1, -1, 1, -1, 0, 0, -1, 1, 1, 1]
    return out


class DecodeTests(unittest.TestCase):
    def test_a_strong_cell_becomes_a_box_with_five_landmarks_in_the_pictures_pixels(self):
        found = fm.decode_detections(synthetic_outputs(10, 20, 16, 0.9))
        self.assertEqual(len(found), 1)
        score, box, points = found[0]
        self.assertAlmostEqual(score, 0.9, places=5)  # sqrt(0.9 x 0.9)
        cx, cy = (20 + 0.25) * 16, (10 + 0.25) * 16
        np.testing.assert_allclose(box, [cx - 24, cy - 32, cx + 24, cy + 32], atol=1e-3)
        np.testing.assert_allclose(points[2], [20 * 16, 10 * 16], atol=1e-3)  # The nose: no offset from the cell
        np.testing.assert_allclose(points[0], [19 * 16, 9 * 16], atol=1e-3)  # The right eye: a cell up and left

    def test_cells_under_the_threshold_are_ignored_and_overlapping_boxes_collapse(self):
        self.assertEqual(fm.decode_detections(synthetic_outputs(5, 5, 8, 0.4)), [])
        merged = synthetic_outputs(10, 20, 16, 0.9)
        merged["cls_16"][0, 10 * 40 + 21, 0] = merged["obj_16"][0, 10 * 40 + 21, 0] = 0.8  # The next cell along says nearly the same box
        merged["bbox_16"][0, 10 * 40 + 21] = [-0.75, 0.25, np.log(3), np.log(4)]
        self.assertEqual(len(fm.decode_detections(merged)), 1)


class MatchTests(unittest.TestCase):
    def test_cosine_and_the_nearest_watch_entry_over_the_threshold(self):
        a, b = np.array([1.0, 0, 0]), np.array([0, 1.0, 0])
        self.assertAlmostEqual(fm.cosine(a, a * 5), 1.0)
        self.assertAlmostEqual(fm.cosine(a, b), 0.0)
        entries = [("x", [b]), ("y", [np.array([0.9, 0.1, 0])])]
        found = fm.match(np.array([1.0, 0.05, 0]), entries)
        self.assertEqual(found[0], "y")
        self.assertGreater(found[1], 0.9)
        self.assertIsNone(fm.match(np.array([0, 0, 1.0]), entries))  # Like neither
        self.assertIsNone(fm.match(a, []))
        self.assertEqual(fm.match(np.array([0.7, 0.7, 0]), entries, threshold=0.6)[0], "y")


class FakeCore:
    """Stands in for OpenVINO: a detector that returns one synthetic face and a recogniser that returns a fixed embedding."""

    def __init__(self, embedding: np.ndarray):
        self.embedding = embedding

    def read_model(self, path: str) -> str:
        return path

    def compile_model(self, model: str, provider: str) -> Any:
        core = self

        class Port:
            def __init__(self, name: str):
                self.name = name

            def get_names(self) -> set[str]:
                return {self.name, "7"}  # The ONNX name and a graph number, as OpenVINO reports them

        class Model:
            def __call__(self, tensor: np.ndarray) -> dict[Any, np.ndarray]:
                if "yunet" in model:
                    return {Port(name): value for name, value in synthetic_outputs(10, 20, 16, 0.9).items()}
                return {Port("fc1"): core.embedding.reshape(1, -1)}

        return Model()


class ReaderTests(unittest.TestCase):
    def reader(self, embedding=(3.0, 4.0)):
        return fm.FaceReader(Path("/m/yunet.onnx"), Path("/m/sface.onnx"), core=FakeCore(np.array(embedding)))

    def test_a_face_comes_back_in_fractions_of_the_picture_with_a_unit_embedding(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)  # Scale 1: the 640-wide picture fills the detector's width
        faces = self.reader().faces(frame)
        self.assertEqual(len(faces), 1)
        face = faces[0]
        cx, cy = (20 + 0.25) * 16, (10 + 0.25) * 16
        self.assertAlmostEqual(face.box[0], (cx - 24) / 640, places=4)
        self.assertAlmostEqual(face.box[3], (cy + 32) / 480, places=4)
        self.assertEqual(face.pixels, (48, 64))
        self.assertAlmostEqual(float(np.linalg.norm(face.embedding)), 1.0)
        np.testing.assert_allclose(face.embedding, [0.6, 0.8])

    def test_a_picture_larger_than_the_detector_maps_the_face_back_to_its_own_size(self):
        frame = np.zeros((960, 1280, 3), dtype=np.uint8)  # Scale 0.5
        face = self.reader().detect(frame)[0]
        self.assertEqual(face.pixels, (96, 128))  # The 48 x 64 found in the half-size picture
        self.assertAlmostEqual(face.box[0], (((20 + 0.25) * 16 - 24) / 0.5) / 1280, places=4)

    def test_cameras_looking_for_faces_at_the_same_moment_each_get_their_faces(self):
        # One face reader serves every face-watch camera, each from its own worker thread; each model refuses overlapping calls (#436).
        from concurrent.futures import ThreadPoolExecutor
        from tests.test_plate_regions import OneRequest
        reader = self.reader()
        reader.detector, reader.recogniser = OneRequest(reader.detector), OneRequest(reader.recogniser)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: reader.faces(frame), range(48)))
        self.assertEqual([len(found) for found in results], [1] * 48)
        np.testing.assert_allclose(results[-1][0].embedding, [0.6, 0.8])

    def test_a_face_narrower_than_the_minimum_is_found_but_not_recognised(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        self.assertEqual(len(self.reader().detect(frame)), 1)
        self.assertEqual(self.reader().faces(frame, minimum_pixels=60), [])


if __name__ == "__main__":
    unittest.main()
