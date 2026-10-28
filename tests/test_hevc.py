"""HEVC playback and hardware acceleration (#133): H.265 plays natively where the browser can and through a shared H.264 conversion where not,
conversions count against slots, and the machine's acceleration is found, used, fallen back from and shown. The CI machine has none: software runs."""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import acceleration, live_hub
from app.api import live_media
from app.application import create_app
from app.credentials import encrypt
from app.live_hub import Fragmenter, LiveStream, codec_string, hevc_codec
from app.store import connect
from tests.test_support import TEST_PASSWORD

FFMPEG = shutil.which("ffmpeg")
HAS_X265 = bool(FFMPEG) and "libx265" in subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


def hvcc(profile_space=0, tier=0, profile=1, compatibility=0x60000000, constraints=bytes([0xB0, 0, 0, 0, 0, 0]), level=93) -> bytes:
    """An hvcC box payload as far as the codec string needs it, inside an init-like byte string."""
    record = bytes([1, (profile_space << 6) | (tier << 5) | profile]) + struct.pack(">I", compatibility) + constraints + bytes([level])
    return b"\0" * 8 + b"hvc1" + b"\0" * 8 + b"hvcC" + record + b"\0" * 8


class FakeFfmpeg:
    """A stand-in for running FFmpeg: lists these encoders and says whether a test encode with a given codec works."""

    def __init__(self, encoders: str = "", working: tuple[str, ...] = ()) -> None:
        self.encoders, self.working, self.calls = encoders, working, []

    def __call__(self, args: list[str], timeout: int) -> tuple[int, str]:
        self.calls.append(args)
        if "-encoders" in args:
            return 0, self.encoders
        return (0, "") if any(codec in args for codec in self.working) else (1, "")


ALL_ENCODERS = " V....D h264_nvenc NVENC\n V..... h264_qsv QSV\n V....D h264_vaapi VAAPI\n V....D libx264 x264\n"


class AccelerationTests(unittest.TestCase):
    def detect(self, runner: FakeFfmpeg, devices: tuple[str, ...] = ("nvenc", "qsv", "vaapi"), **environ: str) -> dict:
        return acceleration.detect(runner, environ=environ, device_present=lambda name: name in devices, ffmpeg="/usr/bin/ffmpeg")

    def test_software_is_used_when_there_is_nothing_else_and_it_says_why(self):
        found = self.detect(FakeFfmpeg("libx264 only"), devices=())
        self.assertEqual((found["selected"], found["ffmpeg"], found["setting"]), (None, True, "auto"))
        self.assertEqual([(entry["name"], entry["encoder"], entry["usable"]) for entry in found["backends"]], [("nvenc", False, False), ("qsv", False, False), ("vaapi", False, False)])
        self.assertIn("software", found["note"])

    def test_a_device_without_the_encoder_or_an_encoder_without_a_device_is_not_chosen(self):
        found = self.detect(FakeFfmpeg(ALL_ENCODERS), devices=("nvenc",))
        by_name = {entry["name"]: entry for entry in found["backends"]}
        self.assertEqual((found["selected"], by_name["qsv"]["device"], by_name["qsv"]["usable"]), (None, False, False))
        self.assertIn("no /dev/dri render device", by_name["vaapi"]["detail"])
        found = self.detect(FakeFfmpeg(" V..... h264_vaapi VAAPI"), devices=("vaapi",), NVR_HWACCEL="nvenc")
        self.assertEqual(found["backends"][0]["detail"], "This FFmpeg has no h264_nvenc encoder")

    def test_a_listed_device_is_only_chosen_if_a_test_encode_works(self):
        broken = self.detect(FakeFfmpeg(ALL_ENCODERS, working=()))
        self.assertEqual(broken["selected"], None)
        self.assertTrue(all(entry["device"] and not entry["usable"] for entry in broken["backends"]))
        self.assertIn("test encode failed", broken["backends"][0]["detail"])
        working = self.detect(FakeFfmpeg(ALL_ENCODERS, working=("h264_vaapi",)))
        self.assertEqual(working["selected"], "vaapi")
        self.assertEqual([(entry["name"], entry["usable"]) for entry in working["backends"]], [("nvenc", False), ("qsv", False), ("vaapi", True)])

    def test_the_first_working_backend_in_order_of_preference_wins_and_the_setting_can_force_or_forbid_one(self):
        everything = FakeFfmpeg(ALL_ENCODERS, working=("h264_nvenc", "h264_qsv", "h264_vaapi"))
        self.assertEqual(self.detect(everything)["selected"], "nvenc")
        self.assertEqual(self.detect(FakeFfmpeg(ALL_ENCODERS, working=("h264_qsv", "h264_vaapi")))["selected"], "qsv")
        forced = self.detect(FakeFfmpeg(ALL_ENCODERS, working=("h264_nvenc", "h264_vaapi")), NVR_HWACCEL="vaapi")
        self.assertEqual((forced["selected"], [entry["name"] for entry in forced["backends"]]), ("vaapi", ["vaapi"]))
        off = FakeFfmpeg(ALL_ENCODERS, working=("h264_nvenc",))
        found = self.detect(off, NVR_HWACCEL="none")
        self.assertEqual((found["selected"], found["backends"], off.calls), (None, [], []))
        self.assertIn("turned off", found["note"])
        odd = self.detect(FakeFfmpeg(ALL_ENCODERS, working=("h264_qsv",)), NVR_HWACCEL="turbo")
        self.assertEqual((odd["selected"], odd["setting"]), ("qsv", "auto"))
        self.assertIn("NVR_HWACCEL=turbo", odd["note"])

    def test_without_ffmpeg_nothing_can_be_found_and_nothing_raises(self):
        with patch("app.acceleration.shutil.which", return_value=None):
            found = acceleration.detect(FakeFfmpeg(), environ={}, device_present=lambda name: True)
        self.assertEqual((found["ffmpeg"], found["selected"]), (False, None))

    def test_each_backend_has_its_own_arguments_and_software_is_libx264(self):
        env = {"NVR_VAAPI_DEVICE": "/dev/dri/renderD129"}
        vaapi = acceleration.build("vaapi", crf=26, environ=env)
        self.assertEqual((vaapi.pre, vaapi.vf_tail, vaapi.codec), (("-vaapi_device", "/dev/dri/renderD129"), "format=nv12,hwupload", ("-c:v", "h264_vaapi", "-qp", "26")))
        nvenc = acceleration.build("nvenc", crf=26, speed="ultrafast")
        self.assertEqual((nvenc.pre, nvenc.vf_tail, nvenc.codec[:5]), ((), "", ("-c:v", "h264_nvenc", "-preset", "p1", "-rc")))
        self.assertEqual(acceleration.build("nvenc", speed="medium").codec[3], "p4")
        qsv = acceleration.build("qsv", crf=26)
        self.assertEqual((qsv.vf_tail, qsv.codec[:2]), ("format=nv12", ("-c:v", "h264_qsv")))
        software = acceleration.build(None, crf=26, speed="ultrafast")
        self.assertEqual((software.name, software.pre, software.vf_tail, software.codec), (None, (), "", ("-c:v", "libx264", "-preset", "ultrafast", "-crf", "26")))
        self.assertEqual(vaapi.filters("scale=1280:720"), "scale=1280:720,format=nv12,hwupload")
        self.assertEqual(vaapi.filters(None), "format=nv12,hwupload")
        self.assertEqual((software.filters("scale=1280:720"), software.filters(None)), ("scale=1280:720", None))
        self.assertEqual(acceleration._decode("vaapi", env), ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD129"])
        self.assertEqual((acceleration._decode("nvenc", {}), acceleration._decode("qsv", {}), acceleration._decode(None, {})), (["-hwaccel", "cuda"], ["-hwaccel", "qsv"], []))

    def test_a_backend_that_fails_in_use_is_turned_off_with_its_reason_until_restart(self):
        acceleration.reset_for_tests()
        self.addCleanup(acceleration.reset_for_tests)
        acceleration._state = {"setting": "auto", "selected": "vaapi", "backends": [], "software_encoder": "libx264", "ffmpeg": True, "note": None, "disabled_reason": None, "checked_at": ""}
        self.assertEqual(acceleration.selected(), "vaapi")
        self.assertEqual(acceleration.encoder().name, "vaapi")
        acceleration.disable("the driver crashed")
        self.assertEqual((acceleration.selected(), acceleration.encoder().name, acceleration.decode_args()), (None, None, []))
        self.assertEqual(acceleration.current()["disabled_reason"], "Turned off after a failure: the driver crashed")
        acceleration.disable("again")  # Nothing left to turn off: the first reason stays
        self.assertIn("the driver crashed", acceleration.current()["disabled_reason"])

    def test_the_real_machine_is_checked_without_error(self):
        found = acceleration.detect()
        self.assertIn("selected", found)
        if found["ffmpeg"] and not any(entry["usable"] for entry in found["backends"]):
            self.assertIsNone(found["selected"])  # The CI machine has no graphics acceleration: software runs


class CodecStringTests(unittest.TestCase):
    def test_the_hevc_codec_string_follows_rfc_6381(self):
        self.assertEqual(hevc_codec(hvcc()), "hvc1.1.6.L93.B0")
        self.assertEqual(hevc_codec(hvcc(profile=2, compatibility=0x20000000, level=120)), "hvc1.2.4.L120.B0")
        self.assertEqual(hevc_codec(hvcc(tier=1, constraints=bytes([0x90, 0, 0, 0, 0, 0]))), "hvc1.1.6.H93.90")
        self.assertEqual(hevc_codec(hvcc(profile_space=1, constraints=bytes([0xB0, 0x01, 0, 0, 0, 0]))), "hvc1.A1.6.L93.B0.01")
        self.assertEqual(hevc_codec(hvcc(constraints=bytes(6))), "hvc1.1.6.L93")
        self.assertEqual(hevc_codec(hvcc().replace(b"hvc1", b"hev1")), "hev1.1.6.L93.B0")
        self.assertIsNone(hevc_codec(b"\0" * 64))
        self.assertIsNone(hevc_codec(b"hvcC" + b"\1" * 5))
        self.assertEqual(codec_string(hvcc()), 'video/mp4; codecs="hvc1.1.6.L93.B0"')
        self.assertEqual(codec_string(hvcc() + b"mp4a"), 'video/mp4; codecs="hvc1.1.6.L93.B0, mp4a.40.2"')

    @unittest.skipUnless(HAS_X265, "An H.265 encoder is required")
    def test_a_real_h265_stream_gets_a_playable_codec_string(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "live.mp4"
            subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x240:r=10", "-t", "2", "-c:v", "libx265", "-tag:v", "hvc1",
                            "-x265-params", "keyint=10:min-keyint=10:log-level=none", "-pix_fmt", "yuv420p", "-an", "-f", "mp4",
                            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "300000", str(output)], check=True, timeout=60)
            fragmenter = Fragmenter()
            fragmenter.feed(output.read_bytes())
        self.assertRegex(codec_string(fragmenter.init), r'^video/mp4; codecs="hvc1\.1\.6\.L\d+\.[0-9A-F]+"$')


class CommandTests(unittest.TestCase):
    """What the live hub asks FFmpeg to do in each mode."""

    def arguments(self, hardware: bool = False, **options) -> list[str]:
        return LiveStream(("cam",), "rtsp://camera.invalid/main", False, **options)._arguments("/usr/bin/ffmpeg", hardware)

    def test_h264_is_copied_and_h265_is_copied_with_the_tag_safari_needs(self):
        plain = self.arguments()
        self.assertEqual(plain[plain.index("-c:v") + 1], "copy")
        self.assertNotIn("-tag:v", plain)
        hevc = self.arguments(hevc_copy=True)
        self.assertEqual((hevc[hevc.index("-c:v") + 1], hevc[hevc.index("-tag:v") + 1]), ("copy", "hvc1"))
        self.assertNotIn("libx264", hevc)

    def test_h265_is_converted_to_h264_in_software_by_default(self):
        converted = self.arguments(transcode=True)
        self.assertEqual(converted[converted.index("-c:v") + 1], "libx264")
        self.assertIn("zerolatency", converted)
        self.assertNotIn("-hwaccel", converted)
        self.assertNotIn("-vf", converted)

    def test_with_a_device_the_conversion_decodes_and_encodes_on_it(self):
        acceleration.reset_for_tests()
        self.addCleanup(acceleration.reset_for_tests)
        acceleration._state = {"setting": "auto", "selected": "vaapi", "backends": [], "software_encoder": "libx264", "ffmpeg": True, "note": None, "disabled_reason": None, "checked_at": ""}
        with patch.dict(os.environ, {"NVR_VAAPI_DEVICE": "/dev/dri/renderD129"}):
            converted = self.arguments(hardware=True, transcode=True)
            masked = self.arguments(hardware=True, video_filter="drawbox=x=0:y=0:w=10:h=10:color=black:t=fill")
        before_input = converted[:converted.index("-i")]
        self.assertIn("-vaapi_device", before_input)
        self.assertEqual(before_input[before_input.index("-hwaccel"):before_input.index("-hwaccel") + 4], ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD129"])
        self.assertEqual(converted[converted.index("-c:v") + 1], "h264_vaapi")
        self.assertEqual(converted[converted.index("-vf") + 1], "format=nv12,hwupload")
        self.assertEqual(masked[masked.index("-vf") + 1], "drawbox=x=0:y=0:w=10:h=10:color=black:t=fill,format=nv12,hwupload")
        self.assertEqual(self.arguments(hardware=False, transcode=True)[self.arguments(hardware=False, transcode=True).index("-c:v") + 1], "libx264")
        # A plain copy never touches the device, whatever was found.
        copied = self.arguments(hardware=True)
        self.assertNotIn("-hwaccel", copied)
        self.assertEqual(copied[copied.index("-c:v") + 1], "copy")


@unittest.skipUnless(HAS_X265, "An H.265 encoder is required")
class HevcLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.footage = Path(cls.folder.name) / "hevc.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=640x360:r=10", "-t", "20", "-an", "-c:v", "libx265", "-tag:v", "hvc1",
                        "-x265-params", "keyint=10:min-keyint=10:log-level=none", "-pix_fmt", "yuv420p", "-y", str(cls.footage)], check=True, timeout=120)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_RECORDINGS_PATH")}
        os.environ.update(NVR_DB_PATH=str(root / "state.sqlite3"), NVR_CREDENTIAL_KEY_PATH=str(root / "credentials.key"), NVR_RECORDINGS_PATH=str(root / "recordings"))
        (root / "recordings").mkdir()
        acceleration.reset_for_tests()
        self.addCleanup(acceleration.reset_for_tests)
        self.feeds: list[subprocess.Popen] = []
        # Cameras are fed through named pipes at real-time speed, since there is no RTSP server here; the hub reads them as it would a camera.
        for patcher in (patch.object(live_hub, "_PROTOCOLS", live_hub._PROTOCOLS + ",file"), patch.object(live_media, "safe_stream_url", lambda value: value)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = create_app(start_workers=False)
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()
        self.assertEqual(self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 201)
        self.add_camera("cam", "Yard", feed=True)

    def tearDown(self):
        self.context.__exit__(None, None, None)
        for feed in self.feeds:
            feed.kill()
            feed.wait()
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def add_camera(self, camera_id: str, name: str, codec: str = "hevc", feed: bool = False) -> str:
        pipe = Path(self.temporary.name) / f"{camera_id}.ts"
        if feed:
            os.mkfifo(pipe)
            self.feeds.append(subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-re", "-stream_loop", "-1", "-i", str(self.footage), "-c", "copy", "-f", "mpegts", "-y", str(pipe)],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,video_codec) VALUES(?,?,?,'motion',?)", (camera_id, name, encrypt(f"file:{pipe}"), codec))
        return str(pipe)

    def receive(self, websocket, fragments: int = 4) -> tuple[dict, bytes, list[bytes]]:
        init = websocket.receive_json()
        self.assertEqual(init["type"], "init", init)
        segment = websocket.receive_bytes()
        return init, segment, [websocket.receive_bytes() for _ in range(fragments)]

    def codec_of(self, segment: bytes, fragments: list[bytes]) -> str:
        media = Path(self.temporary.name) / f"received-{time.monotonic_ns()}.mp4"
        media.write_bytes(segment + b"".join(fragments))
        probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=codec_name,nb_read_frames", "-of", "json", str(media)],
                               capture_output=True, text=True, check=True)
        stream = json.loads(probe.stdout)["streams"][0]
        self.assertGreater(int(stream["nb_read_frames"]), 0)
        return stream["codec_name"]

    def wait_for(self, condition, seconds: float = 8) -> bool:
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.05)
        return condition()

    def test_a_browser_that_plays_h265_gets_it_as_it_is(self):
        with self.client.websocket_connect("/api/cameras/cam/live/stream?hevc=true") as websocket:
            init, segment, fragments = self.receive(websocket)
            self.assertEqual((init["codec"], init["transcoded"], init["accelerated"]), ("hevc", False, False))
            self.assertRegex(init["mime"], r'^video/mp4; codecs="hvc1\.1\.6\.L\d+\.[0-9A-F]+"$')
            self.assertEqual(self.codec_of(segment, fragments), "hevc")
            self.assertEqual(live_media.transcode_counts(), (0, 0))  # Nothing was converted

    def test_any_other_browser_gets_h264_converted_here_and_never_mjpeg(self):
        with self.client.websocket_connect("/api/cameras/cam/live/stream") as websocket:
            init, segment, fragments = self.receive(websocket)
            self.assertEqual((init["codec"], init["transcoded"], init["accelerated"]), ("h264", True, False))
            self.assertRegex(init["mime"], r'^video/mp4; codecs="avc1\.')
            self.assertEqual(self.codec_of(segment, fragments), "h264")
            self.assertEqual(live_media.transcode_counts(), (1, 1))
            self.assertEqual([(entry["transcoded"], entry["accelerated"]) for entry in self.app.state.live_hub.snapshot()], [(True, False)])

    def test_several_viewers_share_one_conversion_and_it_ends_when_they_leave(self):
        hub = self.app.state.live_hub
        with patch("app.live_hub.LINGER_SECONDS", 0.3):
            with self.client.websocket_connect("/api/cameras/cam/live/stream") as first:
                self.receive(first, 2)
                with self.client.websocket_connect("/api/cameras/cam/live/stream") as second:
                    init, _, fragments = self.receive(second, 2)
                    self.assertTrue(init["transcoded"])
                    [stream] = hub.snapshot()
                    self.assertEqual((stream["viewers"], stream["camera_connections"], stream["transcoded"]), (2, 1, True))
                    self.assertEqual(live_media.transcode_counts(), (1, 2))
        self.assertTrue(self.wait_for(lambda: live_media.transcode_counts() == (0, 0)), "conversion slots are released on exit")
        self.assertTrue(self.wait_for(lambda: not hub.snapshot()), "and the conversion stops")

    def test_converted_viewers_count_against_the_slots_and_a_new_conversion_needs_a_free_one(self):
        self.add_camera("two", "Gate", feed=True)
        with patch.object(live_media, "MAX_LIVE_TRANSCODES", 1):
            with self.client.websocket_connect("/api/cameras/cam/live/stream") as first:
                self.receive(first, 1)
                with self.client.websocket_connect("/api/cameras/cam/live/stream") as more:  # Another viewer of the same conversion is free
                    self.receive(more, 1)
                    self.assertEqual(live_media.transcode_counts(), (1, 2))
                with self.client.websocket_connect("/api/cameras/two/live/stream") as refused:
                    self.assertEqual(refused.receive_json()["type"], "busy")
                with self.client.websocket_connect("/api/cameras/two/live/stream?hevc=true") as native:  # H.265 passed on needs none
                    self.assertEqual(native.receive_json()["type"], "init")
            self.assertTrue(self.wait_for(lambda: live_media.transcode_counts() == (0, 0)))
            self.assertTrue(self.wait_for(lambda: live_media._live_view_count == 0))
            with self.client.websocket_connect("/api/cameras/two/live/stream") as again:
                self.assertEqual(again.receive_json()["type"], "init")

    def test_a_refused_viewer_slot_leaves_no_conversion_counted(self):
        with patch.object(live_media, "MAX_LIVE_VIEWERS", 1):
            with self.client.websocket_connect("/api/cameras/cam/live/stream") as first:
                self.receive(first, 1)
                with self.client.websocket_connect("/api/cameras/cam/live/stream") as refused:
                    self.assertEqual(refused.receive_json()["type"], "busy")
                self.assertEqual(live_media.transcode_counts(), (1, 1))
        self.assertTrue(self.wait_for(lambda: live_media.transcode_counts() == (0, 0)))

    def test_mjpeg_is_offered_only_when_the_conversion_and_the_pass_through_both_fail(self):
        self.add_camera("broken", "Dead", feed=False)  # Its pipe does not exist: nothing can be read from it
        with self.client.websocket_connect("/api/cameras/broken/live/stream") as websocket:
            message = websocket.receive_json()
            self.assertEqual((message["type"], message["fallback"], message["converted"]), ("unsupported", "/api/cameras/broken/live?quality=low", True))
            with self.assertRaises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            self.assertEqual(closed.exception.code, 4415)
        with self.client.websocket_connect("/api/cameras/broken/live/stream?hevc=true") as websocket:
            self.assertEqual(websocket.receive_json()["type"], "unsupported")
        self.assertEqual(live_media.transcode_counts(), (0, 0))
        # Other codecs the browser cannot play are refused to the fallback as before.
        self.add_camera("old", "Old", codec="mjpeg", feed=False)
        with self.client.websocket_connect("/api/cameras/old/live/stream") as websocket:
            self.assertEqual(websocket.receive_json()["type"], "unsupported")

    def test_a_dead_camera_is_not_blamed_on_the_device(self):
        acceleration._state = {"setting": "auto", "selected": "vaapi", "backends": [], "software_encoder": "libx264", "ffmpeg": True, "note": None, "disabled_reason": None, "checked_at": ""}
        self.add_camera("broken", "Dead", feed=False)
        with self.client.websocket_connect("/api/cameras/broken/live/stream") as websocket:
            self.assertEqual(websocket.receive_json()["type"], "unsupported")
        self.assertEqual(acceleration.selected(), "vaapi")

    def test_the_low_quality_stream_of_an_h265_camera_is_converted_too(self):
        with self.client.websocket_connect("/api/cameras/cam/live/stream?quality=low") as websocket:
            init, _, _ = self.receive(websocket, 1)
        self.assertEqual((init["codec"], init["transcoded"]), ("h264", True))

    def test_a_device_that_fails_is_given_up_on_and_the_viewer_still_gets_video_in_software(self):
        acceleration._state = {"setting": "auto", "selected": "vaapi", "backends": [], "software_encoder": "libx264", "ffmpeg": True, "note": None, "disabled_reason": None, "checked_at": ""}
        with patch.dict(os.environ, {"NVR_VAAPI_DEVICE": "/dev/dri/does-not-exist"}):
            with self.client.websocket_connect("/api/cameras/cam/live/stream") as websocket:
                init, segment, fragments = self.receive(websocket, 3)
        self.assertEqual((init["transcoded"], init["accelerated"], self.codec_of(segment, fragments)), (True, False, "h264"))
        found = acceleration.current()
        self.assertEqual(found["selected"], None)
        self.assertIn("Turned off after a failure", found["disabled_reason"])

    def test_system_status_reports_the_acceleration_found_and_the_conversions_running(self):
        status = self.client.get("/api/system/status").json()
        self.assertIn(status["acceleration"]["selected"], (None, "nvenc", "qsv", "vaapi"))
        self.assertTrue(status["acceleration"]["ffmpeg"])
        self.assertEqual(status["live"], {"viewers": 0, "audio_listeners": 0, "conversions": 0, "converted_viewers": 0})
        with self.client.websocket_connect("/api/cameras/cam/live/stream") as websocket:
            self.receive(websocket, 1)
            live = self.client.get("/api/system/status").json()["live"]
            self.assertEqual((live["viewers"], live["conversions"], live["converted_viewers"]), (1, 1, 1))

    def test_a_masked_hevc_camera_is_converted_to_h264_as_masks_always_were(self):
        with connect() as db:
            db.execute("INSERT INTO privacy_masks(camera_id,zones_json,camera_side,camera_mask_tokens_json,updated_at) VALUES('cam',?,'none','[]','2026-10-20T00:00:00Z')",
                       (json.dumps([{"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}]),))
        with self.client.websocket_connect("/api/cameras/cam/live/stream?hevc=true") as websocket:
            init, segment, fragments = self.receive(websocket, 3)
            self.assertEqual((init["codec"], init["transcoded"], self.codec_of(segment, fragments)), ("h264", False, "h264"))
            self.assertEqual(live_media.transcode_counts(), (0, 0))  # Masking converted it already; that was never limited by this


@unittest.skipUnless(HAS_X265, "An H.265 encoder is required")
class HevcPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_RECORDINGS_PATH")}
        os.environ.update(NVR_DB_PATH=str(root / "state.sqlite3"), NVR_CREDENTIAL_KEY_PATH=str(root / "credentials.key"), NVR_RECORDINGS_PATH=str(root / "recordings"))
        (root / "recordings" / "cam").mkdir(parents=True)
        acceleration.reset_for_tests()
        self.addCleanup(acceleration.reset_for_tests)
        self.app = create_app(start_workers=False)
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()
        self.assertEqual(self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 201)
        self.file = root / "recordings" / "cam" / "hevc.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x240:r=10", "-t", "2", "-an", "-c:v", "libx265", "-tag:v", "hvc1",
                        "-x265-params", "log-level=none", "-pix_fmt", "yuv420p", "-y", str(self.file)], check=True, timeout=60)
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,video_codec) VALUES('cam','Yard',?,'motion','hevc')", (encrypt("rtsp://camera.invalid/main"),))
            self.segment = db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES('cam','cam/hevc.mp4','2026-10-20T10:00:00+00:00','2026-10-20T10:00:02+00:00',?)",
                                      (self.file.stat().st_size,)).lastrowid

    def tearDown(self):
        self.context.__exit__(None, None, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def codec(self, content: bytes) -> str:
        media = Path(self.temporary.name) / f"played-{time.monotonic_ns()}.mp4"
        media.write_bytes(content)
        return json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "json", str(media)],
                                         capture_output=True, text=True, check=True).stdout)["streams"][0]["codec_name"]

    def test_a_recording_is_served_as_it_is_and_converted_to_h264_when_asked(self):
        url = f"/api/cameras/cam/segments/{self.segment}/media"
        plain = self.client.get(url)
        self.assertEqual((plain.status_code, self.codec(plain.content)), (200, "hevc"))
        converted = self.client.get(url + "?compat=h264")
        self.assertEqual((converted.status_code, converted.headers["content-type"], self.codec(converted.content)), (200, "video/mp4", "h264"))
        copies = Path(self.temporary.name) / "recordings" / ".masked"
        before = sorted(item.name for item in copies.glob("*.mp4"))
        again = self.client.get(url + "?compat=h264")
        self.assertEqual(again.content, converted.content)
        self.assertEqual(sorted(item.name for item in copies.glob("*.mp4")), before)  # Made once, then shared
        self.assertEqual(len(before), 1)
        self.assertEqual(self.client.get(url + "?compat=vp9").status_code, 422)

    def test_a_conversion_that_fails_on_the_device_is_done_again_in_software(self):
        acceleration._state = {"setting": "auto", "selected": "vaapi", "backends": [], "software_encoder": "libx264", "ffmpeg": True, "note": None, "disabled_reason": None, "checked_at": ""}
        with patch.dict(os.environ, {"NVR_VAAPI_DEVICE": "/dev/dri/does-not-exist"}):
            converted = self.client.get(f"/api/cameras/cam/segments/{self.segment}/media?compat=h264")
        self.assertEqual((converted.status_code, self.codec(converted.content)), (200, "h264"))
        self.assertIsNone(acceleration.current()["selected"])
        self.assertIn("Turned off", acceleration.current()["disabled_reason"])

    def test_the_timeline_says_what_codec_each_camera_records(self):
        timeline = self.client.post("/api/playback/timeline", json={"camera_ids": ["cam"], "start": "2026-10-20T09:59:00Z", "end": "2026-10-20T10:01:00Z"}).json()
        self.assertEqual(timeline["cameras"][0]["video_codec"], "hevc")


if __name__ == "__main__":
    unittest.main()
