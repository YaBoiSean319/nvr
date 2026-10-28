"""The plate pipeline against the real OpenVINO runtime with stand-in models (see tests/fixtures/plate_models.py): not the Open Model Zoo models, so these tests
prove the code (loading, shapes, charset file, CTC decoding, scanner, storage, search) and say nothing about accuracy on a real camera."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app import plates
from tests.fixtures import plate_accuracy, plate_models as models
from tests.test_plates import PlateCase

try:
    READY = models.available()
except Exception:  # noqa: BLE001
    READY = False


@unittest.skipUnless(READY, "needs FFmpeg with drawtext and a TrueType font to draw plates")
class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.detector, cls.recogniser = models.build_models(Path(cls.directory.name))
        cls.reader = plates.PlateReader(cls.detector, cls.recogniser)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_the_reader_loads_real_models_with_their_charset_and_reads_a_plate_in_a_full_size_frame(self):
        self.assertEqual((self.reader.input_height, self.reader.input_width), (models.INPUT_H, models.INPUT_W))
        self.assertEqual(self.reader.symbols[-1], "<blank>")
        found = self.reader.read(models.frame_with(models.render("AB12CDE")))
        self.assertEqual([plate.text for plate in found], ["AB12CDE"])
        self.assertGreater(found[0].confidence, 0.5)
        self.assertAlmostEqual(found[0].box["x"], models.PLATE_BOX[0], places=3)
        self.assertEqual(found[0].crop.shape[:2], (models.PLATE_H, models.PLATE_W))
        self.assertEqual([plate.text for plate in self.reader.read(models.frame_with(models.render("AAB0O11")))], ["AAB0O11"])  # A repeated character survives the CTC collapse

    def test_the_models_are_found_by_their_files(self):
        self.assertTrue(plates.models_available(self.detector, self.recogniser))
        self.assertFalse(plates.models_available(self.detector, Path(self.directory.name) / "missing.xml"))

    def test_a_labelled_sample_is_scored_from_pictures_and_degrades_with_picture_quality(self):
        texts = plate_accuracy.sample(60, 5)
        with tempfile.TemporaryDirectory() as directory:
            labels = plate_accuracy.write_images(texts, Path(directory))
            report = plates.evaluate(labels, Path(directory), self.reader)
        self.assertEqual((report["plates"], report["read"], report["exact"]), (60, 60, 60))
        levels = dict(plate_accuracy.measure(self.reader, texts))
        self.assertEqual(levels["Clean"]["exact_percent"], 100.0)
        self.assertLess(levels["Plate 4 px off, noise 35"]["exact_percent"], levels["Plate 2 px off, noise 35"]["exact_percent"])
        self.assertLess(levels["Blur 4, low contrast 35%, noise 60, 2 px off"]["exact_percent"], 60.0)
        for row in levels.values():
            self.assertGreaterEqual(row["lookalike_tolerant"], row["exact"])  # Forgiving look-alikes can only find more

    def test_a_vehicle_gets_its_plate_read_by_the_real_models_and_found_by_part_of_it(self):
        case = PlateCase("run")
        case.setUp()
        try:
            camera = case.d1
            # A vehicle whose box covers the plate's place in the frame
            box = {"x": 0.3, "y": 0.5, "width": 0.4, "height": 0.4}
            track = case.vehicle(camera, box=box)

            async def snapshot():
                return models.frame_with(models.render("KT55QRS"))

            scanner = plates.PlateScanner(self.reader, snapshot, lambda track_id, plate: plates.keep_plate(camera, track_id, plate, datetime.now(timezone.utc), self.reader.reader_id), interval=0.0)
            scanner.seen(track, "vehicle", box)
            self.assertEqual(asyncio.run(scanner.scan()), 1)
            found = case.search("55Q")
            self.assertEqual([(item["plate"], item["track_id"], item["has_crop"]) for item in found], [("KT55QRS", track, True)])
            self.assertEqual(case.admin.get(f"/api/plates/reads/{found[0]['id']}/crop").content[:2], b"\xff\xd8")
        finally:
            case.tearDown()


if __name__ == "__main__":
    unittest.main()
