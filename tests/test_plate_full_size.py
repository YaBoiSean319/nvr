"""Plates are read from the camera's full-size frame (#142): the snapshot is not scaled, the detector still looks at a frame no wider than it always did, and the crop it
reads is cut from the full-size frame."""
from __future__ import annotations

import asyncio
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app import plates
from tests.test_plates import FakeCore


def bmp(pixels: np.ndarray, top_down: bool = False) -> bytes:
    """A 24-bit BMP of a BGR array, as ffmpeg writes one."""
    rows, columns = pixels.shape[:2]
    row_bytes = (columns * 3 + 3) // 4 * 4
    body = b"".join(pixels[row].tobytes().ljust(row_bytes, b"\0") for row in (range(rows) if top_down else reversed(range(rows))))
    header = b"BM" + struct.pack("<IHHI", 54 + len(body), 0, 0, 54) + struct.pack("<IiiHHIIiiII", 40, columns, -rows if top_down else rows, 1, 24, 0, len(body), 2835, 2835, 0, 0)
    return header + body


class DecodeBmpTests(unittest.TestCase):
    def test_a_picture_comes_back_as_it_went_in_whatever_its_width_and_row_order(self):
        rng = np.random.default_rng(1)
        for columns in (1, 2, 3, 5, 64):  # Widths that do and do not need row padding
            picture = rng.integers(0, 256, (7, columns, 3), dtype=np.uint8)
            for top_down in (False, True):
                np.testing.assert_array_equal(plates.decode_bmp(bmp(picture, top_down)), picture)

    def test_something_that_is_not_a_24_bit_bmp_is_refused(self):
        good = bmp(np.zeros((4, 4, 3), dtype=np.uint8))
        self.assertIsNone(plates.decode_bmp(b""))
        self.assertIsNone(plates.decode_bmp(b"\xff\xd8" + good[2:]))  # A JPEG's start
        self.assertIsNone(plates.decode_bmp(good[:-5]))  # Cut short
        self.assertIsNone(plates.decode_bmp(good[:28] + struct.pack("<H", 32) + good[30:]))  # 32 bits per pixel


class DetectorViewTests(unittest.TestCase):
    def test_a_frame_up_to_the_old_width_is_left_alone(self):
        for shape in ((720, 1280, 3), (480, 640, 3), (360, 640, 3)):
            frame = np.zeros(shape, dtype=np.uint8)
            self.assertIs(plates.detector_view(frame), frame)

    def test_a_wider_frame_is_averaged_down_to_no_more_than_the_old_width_with_its_shape_kept(self):
        for shape, expected in (((2160, 3840, 3), (720, 1280)), ((1440, 2560, 3), (720, 1280)), ((1080, 1920, 3), (540, 960)), ((1944, 2592, 3), (648, 864))):
            self.assertEqual(plates.detector_view(np.zeros(shape, dtype=np.uint8)).shape[:2], expected)

    def test_each_pixel_of_the_view_is_the_mean_of_its_block(self):
        frame = np.zeros((6, 6, 3), dtype=np.uint8)
        frame[:3, :3] = 90
        frame[:3, 3:] = 30
        shrunk = plates._box_shrink(frame, 3, 3)
        self.assertEqual(shrunk.shape, (2, 2, 3))
        self.assertEqual((int(shrunk[0, 0, 0]), int(shrunk[0, 1, 0]), int(shrunk[1, 0, 0])), (90, 30, 0))


class ResizeTests(unittest.TestCase):
    def test_a_crop_under_twice_the_target_is_resized_as_before(self):
        crop = np.random.default_rng(2).integers(0, 256, (35, 70, 3), dtype=np.uint8)
        rows = np.minimum((np.arange(24) * 35 / 24).astype(int), 34)
        columns = np.minimum((np.arange(94) * 70 / 94).astype(int), 69)
        np.testing.assert_array_equal(plates._resize(crop, 24, 94), crop[rows][:, columns])

    def test_a_large_crop_is_averaged_so_fine_strokes_are_not_skipped(self):
        crop = np.zeros((96, 376, 3), dtype=np.uint8)
        crop[:, 1::4] = 255  # A one-pixel stroke every fourth column: skipping pixels would see none of it, or all of it
        resized = plates._resize(crop, 24, 94)
        self.assertEqual(resized.shape, (24, 94, 3))
        self.assertTrue(np.all(resized[:, :, 0] == resized[0, 0, 0]) and 0 < int(resized[0, 0, 0]) < 255)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is needed")
class SnapshotTests(unittest.TestCase):
    def snapshot_of(self, size: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=5:duration=1", "-pix_fmt", "yuv420p", str(path)], check=True)
            return asyncio.run(plates.ffmpeg_snapshot(str(path)))

    def test_the_snapshot_is_the_streams_own_size_whatever_its_shape(self):
        for size, shape in (("1920x1080", (1080, 1920, 3)), ("640x480", (480, 640, 3)), ("2560x1440", (1440, 2560, 3)), ("1000x562", (562, 1000, 3))):
            frame = self.snapshot_of(size)
            self.assertIsNotNone(frame, size)
            self.assertEqual(frame.shape, shape)
            self.assertGreater(int(frame.max()), 100)  # A picture, not an empty frame

    def test_a_source_that_cannot_be_opened_gives_nothing(self):
        self.assertIsNone(asyncio.run(plates.ffmpeg_snapshot("/nonexistent/stream.mp4")))


class ReaderTests(unittest.TestCase):
    def test_a_plate_on_a_4k_frame_is_read_from_the_4k_crop(self):
        core = FakeCore([(0.1, 0.5, 0.3, 0.6)], ["AB12CDE"])
        reader = plates.PlateReader(Path("/m/vehicle-license-plate-detection-barrier-0106.xml"), Path("/m/recogniser.xml"), core=core)
        found = reader.read(np.zeros((2160, 3840, 3), dtype=np.uint8))
        self.assertEqual([plate.text for plate in found], ["AB12CDE"])
        self.assertEqual(found[0].crop.shape, (216, 768, 3))  # 0.1 high by 0.2 wide of the full-size frame, not of a scaled copy
        self.assertAlmostEqual(found[0].box["width"], 0.2, places=3)


if __name__ == "__main__":
    unittest.main()
