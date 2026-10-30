"""Licence plate models by region (#210): the registry and its pins, the fixed-slot reader against stand-in models and against the real shipped files, the region setting
and how the AI worker follows it."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from app import plate_models, plates
from app.application import create_app
from app.migrations import m0045_plate_region
from app.security import hash_password
from app.store import connect
from tests.fixtures import real_plate_models
from tests.scene_case import SceneTestCase
from tests.test_support import TEST_PASSWORD

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
ROOT = Path(__file__).resolve().parent.parent


def one_hot(text: str, slots: int = 9) -> np.ndarray:
    grid = np.zeros((1, slots, len(ALPHABET)), dtype=np.float32)
    for position in range(slots):
        grid[0, position, ALPHABET.index(text[position] if position < len(text) else "_")] = 0.9
    return grid.reshape(1, -1)


class RegistryTests(unittest.TestCase):
    def test_every_model_is_pinned_by_https_url_and_checksum_and_the_regions_are_complete(self):
        for model in plate_models.SHIPPED:
            self.assertTrue(model.url.startswith("https://github.com/ankandrew/"), model.url)
            self.assertRegex(model.sha256, r"^[0-9a-f]{64}$")
            self.assertGreater(model.size_bytes, 0)
        self.assertEqual({region.id for region in plate_models.REGIONS}, {"worldwide", "europe", "custom"})
        self.assertEqual(plate_models.DEFAULT_REGION, "worldwide")
        for region in plate_models.REGIONS:
            self.assertTrue(region.label and region.covers)
            self.assertEqual(region.id == "custom", region.recogniser is None)

    def test_the_dockerfile_installs_exactly_the_pinned_files(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        for model in plate_models.SHIPPED:
            self.assertIn(model.url, dockerfile)
            self.assertIn(f"{model.sha256}  /opt/nvr/models/plates/{model.name}", dockerfile)

    def test_a_missing_or_altered_file_is_not_installed(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.assertFalse(plate_models.installed(plate_models.EUROPE, folder))
            for model in (plate_models.DETECTOR, plate_models.EUROPE.recogniser, plate_models.EUROPE.config):
                (folder / model.name).write_bytes(b"x" * model.size_bytes)  # The right size, the wrong content
            self.assertFalse(plate_models.installed(plate_models.EUROPE, folder))


class DecodeTests(unittest.TestCase):
    config = plate_models.SlotConfig(9, ALPHABET, "_", 70, 140, True)

    def test_each_slot_reads_its_best_symbol_and_trailing_padding_is_dropped(self):
        text, confidence = plate_models.decode_slots(one_hot("AB12CDE"), self.config)
        self.assertEqual((text, round(confidence, 2)), ("AB12CDE", 0.9))
        self.assertEqual(plate_models.decode_slots(one_hot(""), self.config), ("", 0.0))
        self.assertEqual(plate_models.decode_slots(one_hot("X1"), self.config)[0], "X1")

    def test_a_config_file_is_read_and_a_bad_one_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            good = Path(temporary) / "good.yaml"
            good.write_text("max_plate_slots: 10\nalphabet: '0123456789ABC_'\npad_char: '_'\nimg_height: 64\nimg_width: 128\nimage_color_mode: rgb\n", encoding="utf-8")
            config = plate_models.SlotConfig.read(good)
            self.assertEqual((config.slots, config.height, config.width, config.grayscale), (10, 64, 128, False))
            bad = Path(temporary) / "bad.yaml"
            bad.write_text("max_plate_slots: 10\nalphabet: 'ABC'\npad_char: '_'\nimg_height: 64\nimg_width: 128\n", encoding="utf-8")  # The pad is not in the alphabet
            with self.assertRaises(ValueError):
                plate_models.SlotConfig.read(bad)
            notes = Path(temporary) / "notes.yaml"
            notes.write_text("- not\n- a mapping\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                plate_models.SlotConfig.read(notes)

    def test_the_picture_is_letterboxed_and_a_detection_maps_back_to_it(self):
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[:, :, 2] = 200  # BGR: a red picture
        tensor, scale, left, top = plate_models.letterbox(frame, 640)
        self.assertEqual((tensor.shape, tensor.dtype, scale, left, top), ((1, 3, 640, 640), np.float32, 0.5, 0.0, 140.0))
        self.assertAlmostEqual(float(tensor[0, 0, 320, 320]), 200 / 255, places=3)  # RGB: red first
        self.assertEqual(float(tensor[0, 2, 320, 320]), 0.0)
        self.assertAlmostEqual(float(tensor[0, 0, 10, 10]), 114 / 255, places=3)  # The grey border

    def test_bilinear_resizing_keeps_the_picture(self):
        picture = np.tile(np.linspace(0, 255, 200, dtype=np.float32)[None, :, None], (100, 1, 3))
        smaller = plate_models.resize_bilinear(picture, 50, 80)
        self.assertEqual(smaller.shape, (50, 80, 3))
        self.assertAlmostEqual(float(smaller.mean()), float(picture.mean()), delta=2.0)
        self.assertGreater(float(smaller[0, -1, 0]), float(smaller[0, 0, 0]))


class FakeCompiled:
    def __init__(self, result, shape=None):
        self.result, self.shape = result, shape or [1, 3, 640, 640]

    def __call__(self, tensor):
        class Port:
            def __init__(self, name):
                self.name = name

            def get_any_name(self):
                return self.name
        return {Port("plate"): self.result(tensor) if callable(self.result) else self.result}


class FakeCore:
    def __init__(self, detector_rows, recogniser_scores):
        self.detector_rows, self.recogniser_scores = detector_rows, recogniser_scores

    def read_model(self, path):
        return Path(path).name

    def compile_model(self, name, provider):
        return FakeCompiled(self.detector_rows if "yolo" in name else self.recogniser_scores)


class ReaderTests(unittest.TestCase):
    def reader(self, rows: np.ndarray, scores: np.ndarray) -> plate_models.RegionReader:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / plate_models.WORLDWIDE.config.name).write_text("max_plate_slots: 9\nalphabet: '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_'\npad_char: '_'\nimg_height: 70\nimg_width: 140\n", encoding="utf-8")
            for name in (plate_models.DETECTOR.name, plate_models.WORLDWIDE.recogniser.name):
                (folder / name).write_bytes(b"model")
            return plate_models.RegionReader(plate_models.WORLDWIDE, core=FakeCore(rows, scores), directory=folder)

    def test_the_reader_maps_a_detection_back_to_the_picture_and_reads_the_crop(self):
        # A plate at x 640..780, y 450..500 of a 1280 x 720 picture is, letter-boxed at half size, x 320..390, y 365..390.
        rows = np.array([[0, 320, 365, 390, 390, 0, 0.8], [0, 0, 0, 5, 5, 0, 0.1]], dtype=np.float32)
        reader = self.reader(rows, one_hot("AB12CDE"))
        frame = np.full((720, 1280, 3), 90, dtype=np.uint8)
        found = reader.read(frame)
        self.assertEqual(len(found), 1)  # The weak detection is left out
        read = found[0]
        self.assertEqual(read.text, "AB12CDE")
        self.assertAlmostEqual(read.box["x"], 0.5, places=2)
        self.assertAlmostEqual(read.box["y"], 450 / 720, places=2)
        self.assertAlmostEqual(read.box["width"], 70 / 1280 * 2, places=2)
        self.assertEqual(read.confidence, 0.8)  # The weaker of detection (0.8) and recognition (0.9)
        self.assertEqual(read.crop.shape[2], 3)
        self.assertEqual(reader.reader_id, "yolo-v9-t-640-license-plate-end2end+global_mobile_vit_v2_ocr")

    def test_an_unreadable_crop_is_dropped(self):
        rows = np.array([[0, 320, 365, 390, 390, 0, 0.8]], dtype=np.float32)
        self.assertEqual(self.reader(rows, one_hot("A")).read(np.zeros((720, 1280, 3), dtype=np.uint8)), [])  # One character is not a plate


@unittest.skipUnless(real_plate_models.folder() is not None or real_plate_models.required(), "needs the shipped plate models (network)")
class RealModelTests(unittest.TestCase):
    """The real shipped files, loaded by OpenVINO and run on a drawn plate. This is the contract check: the code and the files agree about shapes, order and decoding."""

    @classmethod
    def setUpClass(cls):
        cls.folder = real_plate_models.folder()
        cls.frame = real_plate_models.scene()

    def test_the_downloaded_files_are_the_pinned_ones_and_load_with_the_shapes_the_code_expects(self):
        import openvino as ov
        core = ov.Core()
        for model in plate_models.SHIPPED:
            self.assertTrue(plate_models.file_ok(model, self.folder), model.name)
        detector = core.read_model(str(self.folder / plate_models.DETECTOR.name))
        self.assertIn("32", str(detector.input(0).element_type))  # float32
        self.assertEqual(list(detector.input(0).shape), [1, 3, plate_models.DETECTOR_SIZE, plate_models.DETECTOR_SIZE])
        self.assertEqual(list(detector.output(0).partial_shape)[-1].get_length(), 7)
        for region in (plate_models.WORLDWIDE, plate_models.EUROPE):
            config = plate_models.SlotConfig.read(self.folder / region.config.name)
            model = core.read_model(str(self.folder / region.recogniser.name))
            shape = list(model.input(0).partial_shape)
            self.assertEqual((shape[1].get_length(), shape[2].get_length(), shape[3].get_length()), (config.height, config.width, 1 if config.grayscale else 3))
            self.assertEqual(list(model.output(0).partial_shape)[-1].get_length(), config.slots * len(config.alphabet))

    def test_a_drawn_plate_is_found_and_read_through_the_real_detector_and_each_regions_recogniser(self):
        if self.frame is None:
            self.skipTest("needs ffmpeg and the DejaVu font to draw the plate")
        for region in (plate_models.WORLDWIDE, plate_models.EUROPE):
            reader = plate_models.RegionReader(region, directory=self.folder)
            found = reader.read(self.frame)
            self.assertEqual([item.text for item in found], ["AB12CDE"], region.id)
            box = found[0].box  # The plate is at x 455..825, y 455..539 of 1280 x 720
            self.assertAlmostEqual(box["x"], 455 / 1280, delta=0.03, msg=region.id)
            self.assertAlmostEqual(box["y"], 455 / 720, delta=0.04, msg=region.id)
            self.assertGreater(found[0].confidence, 0.35)

    def test_the_loader_gives_the_chosen_regions_reader_and_nothing_for_a_region_without_models(self):
        if self.frame is None:
            self.skipTest("needs ffmpeg and the DejaVu font to draw the plate")
        with patch.object(plate_models, "MODELS_DIR", self.folder):
            europe = plates.load_reader("CPU", "europe")
            self.assertEqual(europe.reader_id, "yolo-v9-t-640-license-plate-end2end+european_mobile_vit_v2_ocr")
            self.assertEqual([item.text for item in europe.read(self.frame)], ["AB12CDE"])
        with tempfile.TemporaryDirectory() as empty, patch.object(plate_models, "MODELS_DIR", Path(empty)):
            self.assertIsNone(plates.load_reader("CPU", "europe"))
            self.assertIsNone(plates.load_reader("CPU", "nowhere"))


class RegionApiTests(SceneTestCase):
    def setUp(self):
        super().setUp()

    def region(self) -> str:
        with connect() as db:
            return plates.plate_region(db)

    def audit(self) -> list[str]:
        with connect() as db:
            return [row["details_json"] for row in db.execute("SELECT details_json FROM audit_log WHERE action='plates_region_changed' ORDER BY id")]

    def test_the_regions_are_listed_with_what_they_cover_and_whether_they_are_installed(self):
        state = self.client.get("/api/admin/plates").json()["models"]
        self.assertEqual((state["region"], [item["id"] for item in state["regions"]]), ("worldwide", ["worldwide", "europe", "custom"]))
        europe = next(item for item in state["regions"] if item["id"] == "europe")
        self.assertTrue(europe["label"] and europe["covers"] and europe["size_bytes"] > 1_000_000)
        self.assertEqual((europe["installed"], europe["manual"]), (False, False))  # Nothing is installed in the test environment

    def test_a_region_is_chosen_audited_and_used_and_one_without_models_is_refused(self):
        self.assertEqual(self.client.put("/api/admin/plates/region", json={"region": "mars"}).status_code, 422)
        refused = self.client.put("/api/admin/plates/region", json={"region": "europe"})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("not installed", refused.json()["detail"])
        self.assertEqual(self.region(), "worldwide")
        with patch.object(plate_models, "installed", lambda region, directory=None: region.id != "custom"):
            chosen = self.client.put("/api/admin/plates/region", json={"region": "europe"})
            self.assertEqual((chosen.status_code, chosen.json()), (200, {"region": "europe"}))
            self.assertEqual(self.client.get("/api/admin/plates").json()["models"]["region"], "europe")
            self.assertEqual(self.client.put("/api/admin/plates/region", json={"region": "custom"}).status_code, 409)  # The hand-installed pair is not there
        self.assertEqual(self.region(), "europe")
        self.assertEqual(len(self.audit()), 1)
        self.assertIn('"previous": "worldwide"', self.audit()[0])

    def test_only_administrators_choose_the_region(self):
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('watcher',?,'operator')", (hash_password(TEST_PASSWORD),))
        with TestClient(self.client.app) as operator:
            self.assertEqual(operator.post("/api/auth/login", json={"username": "watcher", "password": TEST_PASSWORD}).status_code, 200)
            self.assertEqual(operator.put("/api/admin/plates/region", json={"region": "worldwide"}).status_code, 403)

    def test_an_installation_with_hand_installed_models_keeps_them_and_a_new_one_reads_worldwide(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for name in ("det.xml", "det.bin", "rec.xml", "rec.bin"):
                (folder / name).write_bytes(b"x")
            with patch.dict(os.environ, {"NVR_PLATE_DETECTOR_MODEL_PATH": str(folder / "det.xml"), "NVR_PLATE_RECOGNISER_MODEL_PATH": str(folder / "rec.xml")}):
                import sqlite3
                for expected, present in (("custom", True), ("worldwide", False)):
                    if not present:
                        (folder / "rec.bin").unlink()
                    db = sqlite3.connect(":memory:")
                    m0045_plate_region.upgrade(db)
                    self.assertEqual(db.execute("SELECT region FROM plate_region_settings").fetchone()[0], expected)


class WorkerFollowsRegionTests(SceneTestCase):
    def test_the_ai_worker_loads_the_region_chosen_in_settings_and_changes_with_it(self):
        with connect() as db:
            db.execute("INSERT INTO plate_settings(camera_id,enabled,min_confidence) VALUES(?,1,0.5)", (self.camera_id,))
        loaded: list[str] = []
        asked = {"count": 0}

        class Reader:
            def __init__(self, region):
                self.reader_id = f"stand-in-{region}"

            def read(self, frame):
                return []

        def load(provider, region):
            loaded.append(region)
            return Reader(region)

        def region_now(db):
            asked["count"] += 1
            return "worldwide" if asked["count"] <= 3 else "europe"

        async def no_snapshot(*args, **kwargs):
            return None

        with patch.object(plates, "load_reader", load), patch.object(plates, "plate_region", region_now), patch.object(plates, "SETTINGS_REFRESH", 0.0), \
                patch.object(plates, "ffmpeg_snapshot", no_snapshot):
            self.analyse_footage()
        self.assertEqual(loaded, ["worldwide", "europe"])  # Each region is loaded once, the second when the setting changed, with no restart


if __name__ == "__main__":
    unittest.main()
