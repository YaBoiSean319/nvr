"""ONVIF media profiles: reading Media1 and Media2 responses, and choosing which profile to record and which to watch (#173)."""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from app.onvif_profiles import MediaProfile, choose_profiles, normalise_encoding, parse_profiles
from tests.fixtures.onvif_camera import NS, OnvifCamera, hanwha_profiles, profile


def media1(items: list[dict]) -> ET.Element:
    return ET.fromstring(f"<s:Envelope {NS}><s:Body><trt:GetProfilesResponse>{''.join(OnvifCamera._media1(item) for item in items)}</trt:GetProfilesResponse></s:Body></s:Envelope>")


def media2(items: list[dict]) -> ET.Element:
    return ET.fromstring(f"<s:Envelope {NS}><s:Body><tr2:GetProfilesResponse>{''.join(OnvifCamera._media2(item) for item in items)}</tr2:GetProfilesResponse></s:Body></s:Envelope>")


def make(token: str, encoding: str, width: int, height: int, fps: float = 30, bitrate: int = 0) -> MediaProfile:
    return MediaProfile(token, token, encoding, width, height, fps, bitrate)


class ParsingTests(unittest.TestCase):
    def test_media1_profiles_carry_codec_size_rate_audio_and_ptz(self):
        found = parse_profiles(media1([profile("p1", "main", "H264", 1920, 1080, 25, 4096, audio=True, ptz=True), profile("p2", "snap", "JPEG", 640, 480, 5)]), "media1")
        self.assertEqual([(item.token, item.name, item.encoding, item.width, item.height, item.fps, item.bitrate_kbps, item.has_audio, item.has_ptz, item.source) for item in found],
                         [("p1", "main", "h264", 1920, 1080, 25.0, 4096, True, True, "media1"), ("p2", "snap", "mjpeg", 640, 480, 5.0, 4096, False, False, "media1")])

    def test_media2_profiles_read_the_same_way(self):
        found = parse_profiles(media2([profile("m1", "main", "H265", 3840, 2160, 20, 8192, audio=True), profile("m2", "sub", "H264", 640, 360, 15)]), "media2")
        self.assertEqual([(item.token, item.encoding, item.width, item.height, item.fps, item.has_audio, item.source) for item in found],
                         [("m1", "h265", 3840, 2160, 20.0, True, "media2"), ("m2", "h264", 640, 360, 15.0, False, "media2")])

    def test_codec_names_from_different_makers_are_one_vocabulary(self):
        for text, expected in (("H264", "h264"), ("h.264", "h264"), ("AVC", "h264"), ("H265", "h265"), ("HEVC", "h265"), ("JPEG", "mjpeg"), ("MJPEG", "mjpeg"),
                               ("MPEG4", "other"), ("", "other"), (None, "other")):
            self.assertEqual(normalise_encoding(text), expected, text)

    def test_a_profile_without_a_video_encoder_is_kept_with_unknown_codec_and_junk_is_ignored(self):
        root = ET.fromstring(f'<s:Envelope {NS}><s:Body><trt:GetProfilesResponse><trt:Profiles token="bare"><tt:Name>bare</tt:Name></trt:Profiles>'
                             '<trt:Profiles><tt:Name>no token</tt:Name></trt:Profiles>'
                             '<trt:Profiles token="odd"><tt:VideoEncoderConfiguration><tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>wide</tt:Width><tt:Height>-</tt:Height></tt:Resolution>'
                             '</tt:VideoEncoderConfiguration></trt:Profiles></trt:GetProfilesResponse></s:Body></s:Envelope>')
        found = parse_profiles(root, "media1")
        self.assertEqual([(item.token, item.encoding, item.pixels) for item in found], [("bare", "other", 0), ("odd", "h264", 0)])


class SelectionTests(unittest.TestCase):
    def test_h264_beats_mjpeg_even_when_mjpeg_is_listed_first_and_is_bigger(self):
        chosen = choose_profiles([make("mjpeg", "mjpeg", 3840, 2160), make("main", "h264", 1920, 1080), make("sub", "h264", 640, 360, 15)])
        self.assertEqual((chosen.record.token, chosen.live.token, chosen.problems), ("main", "sub", ()))
        self.assertIn("MJPEG profiles were not chosen because H.264 is available", chosen.reason)

    def test_the_hanwha_xnv_6120r_profile_set_records_its_h264_main_and_watches_its_h264_sub(self):
        found = parse_profiles(media1(hanwha_profiles()), "media1")
        chosen = choose_profiles(found)
        self.assertEqual((chosen.record.token, chosen.record.encoding, chosen.live.token), ("profile2", "h264", "profile3"))  # Not profile1 (MJPEG), not profile4 (H.265).

    def test_codec_order_is_h264_then_h265_then_mjpeg(self):
        self.assertEqual(choose_profiles([make("a", "mjpeg", 1920, 1080), make("b", "h265", 1280, 720)]).record.token, "b")
        self.assertEqual(choose_profiles([make("a", "h265", 3840, 2160), make("b", "h264", 640, 360)]).record.token, "b")

    def test_mjpeg_alone_is_used_with_a_warning_and_h265_alone_with_one(self):
        only_mjpeg = choose_profiles([make("a", "mjpeg", 1280, 720)])
        self.assertEqual(only_mjpeg.record.token, "a")
        self.assertTrue(only_mjpeg.problems and "MJPEG" in only_mjpeg.problems[0])
        self.assertIn("H.265", choose_profiles([make("a", "h265", 1280, 720)]).problems[0])

    def test_within_a_codec_the_most_pixels_then_frame_rate_then_bit_rate_win(self):
        self.assertEqual(choose_profiles([make("small", "h264", 1280, 720), make("big", "h264", 1920, 1080)]).record.token, "big")
        self.assertEqual(choose_profiles([make("slow", "h264", 1920, 1080, 15), make("fast", "h264", 1920, 1080, 30)]).record.token, "fast")
        self.assertEqual(choose_profiles([make("low", "h264", 1920, 1080, 30, 2000), make("high", "h264", 1920, 1080, 30, 8000)]).record.token, "high")

    def test_the_live_profile_is_a_smaller_h264_one_the_largest_within_720p_else_the_smallest(self):
        record = make("main", "h264", 3840, 2160)
        self.assertEqual(choose_profiles([record, make("a", "h264", 1920, 1080), make("b", "h264", 1280, 720), make("c", "h264", 640, 360)]).live.token, "b")
        self.assertEqual(choose_profiles([record, make("a", "h264", 1920, 1080), make("d", "h264", 2560, 1440)]).live.token, "a")  # None within 720p: the smallest.

    def test_there_is_no_live_profile_when_nothing_smaller_h264_exists(self):
        self.assertIsNone(choose_profiles([make("main", "h264", 1920, 1080)]).live)
        self.assertIsNone(choose_profiles([make("main", "h264", 1920, 1080), make("same", "h264", 1920, 1080)]).live)
        self.assertIsNone(choose_profiles([make("main", "h264", 1920, 1080), make("small", "mjpeg", 320, 240)]).live)
        self.assertIsNone(choose_profiles([make("main", "h265", 1920, 1080), make("small", "h265", 640, 360)]).live)  # Live is H.264 only.

    def test_no_profiles_or_no_usable_codec_says_so(self):
        for found in ([], [make("x", "other", 640, 480)]):
            chosen = choose_profiles(found)
            self.assertEqual((chosen.record, chosen.live, chosen.problems), (None, None, ("No compatible video profile found",)))


if __name__ == "__main__":
    unittest.main()
