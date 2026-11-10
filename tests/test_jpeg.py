"""Plate crops, face pictures and the face-finding frame all go through one JPEG encoder, and each keeps the size it always had (#314)."""
from __future__ import annotations

import shutil
import struct
import unittest
from unittest.mock import patch

import numpy as np

from app import faces, plates
from app.face_models import Face

FFMPEG = shutil.which("ffmpeg")


def jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a JPEG's start-of-frame marker."""
    assert data.startswith(b"\xff\xd8")
    position = 2
    while position < len(data):
        marker = data[position + 1]
        length = struct.unpack(">H", data[position + 2:position + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[position + 5:position + 9])
            return width, height
        position += 2 + length
    raise AssertionError("no frame header")


def picture(width: int, height: int) -> np.ndarray:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, : width // 2] = 220
    return image


@unittest.skipUnless(FFMPEG, "FFmpeg is required")
class SizeTests(unittest.TestCase):
    """What each caller has always produced: crops are widened to at least 160 pixels, whole frames are narrowed to at most 960."""

    def test_a_small_plate_crop_is_widened_in_whole_multiples_and_a_wide_one_is_left_alone(self):
        self.assertEqual(jpeg_size(plates.encode_crop(picture(80, 40))), (160, 80))
        self.assertEqual(jpeg_size(plates.encode_crop(picture(50, 20))), (200, 80))
        self.assertEqual(jpeg_size(plates.encode_crop(picture(200, 100))), (200, 100))

    def test_a_whole_frame_is_narrowed_to_960_and_a_small_one_is_left_alone(self):
        self.assertEqual(jpeg_size(faces.frame_jpeg(picture(1920, 1080))), (960, 540))
        self.assertEqual(jpeg_size(faces.frame_jpeg(picture(640, 480))), (640, 480))

    def test_the_face_picture_is_a_crop_with_a_margin_widened_like_any_crop(self):
        face = Face(score=0.9, box=(0.25, 0.25, 0.5, 0.5), landmarks=((0.3, 0.3),) * 5, pixels=(160, 160))
        width, height = jpeg_size(faces.picture_of(picture(800, 600), face))
        self.assertGreaterEqual(width, 160)
        self.assertGreater(height, 0)

    def test_nothing_that_cannot_be_encoded_makes_a_picture(self):
        self.assertIsNone(plates.encode_crop(None))
        self.assertIsNone(plates.encode_crop(np.zeros((0, 0, 3), dtype=np.uint8)))
        self.assertIsNone(faces.frame_jpeg(np.zeros((0, 0, 3), dtype=np.uint8)))


@unittest.skipUnless(FFMPEG, "FFmpeg is required")
class OneEncoderTests(unittest.TestCase):
    def test_all_three_callers_go_through_the_same_encoder_with_their_own_scale_quality_and_timeout(self):
        from app import jpeg
        calls = []
        real = jpeg.encode

        def spy(image, **settings):
            calls.append(settings)
            return real(image, **settings)

        face = Face(score=0.9, box=(0.25, 0.25, 0.5, 0.5), landmarks=((0.3, 0.3),) * 5, pixels=(160, 160))
        with patch.object(jpeg, "encode", spy):
            plates.encode_crop(picture(80, 40))
            faces.frame_jpeg(picture(1920, 1080))
            faces.picture_of(picture(800, 600), face)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], calls[2])  # A face picture is a crop like a plate's
        self.assertNotEqual(calls[0], calls[1])  # A whole frame is scaled, graded and timed on its own terms
        self.assertEqual(sorted(calls[0]), sorted(calls[1]))


if __name__ == "__main__":
    unittest.main()
