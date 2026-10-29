"""Capacity planner (#149): estimates against hand-worked values, the installation's own cameras as a start, and the assumptions every result lists."""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import capacity
from app.application import create_app
from app.credentials import encrypt
from app.security import hash_password
from app.store import connect
from tests.test_support import TEST_PASSWORD

KEYS = ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_RECORDINGS_PATH", "NVR_STORAGE_VOLUMES")


def row(**changes) -> capacity.Row:
    return capacity.Row(**{"name": "Cameras", "count": 10, "width": 1920, "height": 1080, "codec": "h264", "fps": 15, "bitrate_kbps": 4096, "recording": "continuous", "retention_days": 30} | changes)


class EstimateTests(unittest.TestCase):
    def test_disk_and_network_match_the_hand_worked_values(self):
        # 10 cameras at 4096 kbps, recording all day for 30 days.
        # One camera: 4096 kbps = 512 000 bytes/s, x 86 400 = 44 236 800 000 bytes a day. Ten for 30 days: 13 271 040 000 000.
        result = capacity.estimate([row()], viewers=5, tiles_per_viewer=4)
        metrics = result["metrics"]
        self.assertEqual(result["lines"][0]["bytes_per_day"], 44_236_800_000)
        self.assertEqual(metrics["disk_footage"]["value"], 13_271_040_000_000)
        self.assertAlmostEqual(metrics["disk_needed"]["value"], 13_271_040_000_000 * 1.05, delta=1)  # + overhead
        self.assertAlmostEqual(metrics["disk_recommended"]["value"], 13_271_040_000_000 * 1.05 * 1.2, delta=1)  # + headroom
        self.assertEqual(metrics["write_average"]["value"], 5.12)  # 10 x 4096 kbps / 8 / 1000 MB/s
        self.assertEqual(metrics["write_peak"]["value"], 5.12)
        self.assertEqual(metrics["network_in"]["value"], 40.96)  # 10 x 4096 kbps
        # Five viewers with four tiles each: 20 streams at a quarter of 4096 kbps.
        self.assertEqual(metrics["network_out"]["value"], 20.48)
        # Copying: 40.96 Mbps in x 0.01 + 20.48 Mbps out x 0.005 core per Mbps.
        self.assertAlmostEqual(metrics["cores_streams"]["value"], 0.4096 + 0.1024, places=4)
        self.assertEqual(metrics["cores_conversion"]["value"], 0)
        self.assertEqual(metrics["cores_ai_processor"]["value"], 0)

    def test_assumed_values_are_worked_from_the_defaults_and_labelled(self):
        # 2560 x 1440 is 3.69 megapixels: the 4 MP class, 3072 kbps at 15 fps, x 0.6 for H.265 = 1843.2 kbps. Motion recording keeps 25 % of the day, for 14 days, on 4 cameras.
        plan = capacity.Row(name="Yard", count=4, width=2560, height=1440, codec="h265", fps=15, recording="motion", retention_days=14)
        result = capacity.estimate([plan], viewers=0, tiles_per_viewer=4)
        line = result["lines"][0]
        self.assertEqual((line["bitrate_kbps"], line["bitrate_source"], line["duty"]), (1843.2, "assumed", 0.25))
        self.assertEqual(line["bytes_per_day"], 4_976_640_000)  # 1843.2 x 125 x 86 400 x 0.25
        self.assertEqual(result["metrics"]["disk_footage"]["value"], 278_691_840_000)  # x 14 days x 4 cameras
        self.assertEqual(result["metrics"]["write_average"]["value"], round(4 * 1843.2 * 0.25 / 8 / 1000, 4))
        self.assertEqual(result["metrics"]["write_peak"]["value"], round(4 * 1843.2 / 8 / 1000, 4))
        self.assertTrue(any(text.startswith("bitrate (1843 kbps") for text in line["assumed"]))
        self.assertTrue(any("share of the day recorded (25%" in text for text in line["assumed"]))

    def test_frame_rate_scales_the_default_bitrate_and_a_disabled_camera_stores_nothing(self):
        self.assertEqual(capacity.default_bitrate(1920, 1080, "h264", 30), 4096.0)  # 2048 at 15 fps, doubled
        self.assertEqual(capacity.default_bitrate(1280, 720, "h264", 15), 1024.0)
        self.assertAlmostEqual(capacity.default_bitrate(3840, 2160, "h265", 15), 6144 * 0.6)
        result = capacity.estimate([row(recording="disabled")], 0, 4)
        self.assertEqual((result["metrics"]["disk_footage"]["value"], result["metrics"]["write_peak"]["value"]), (0, 0))
        self.assertEqual(result["metrics"]["network_in"]["value"], 40.96)  # Still connected

    def test_ai_and_conversion_cores(self):
        # Two AI cameras at the default 0.5 analysed frames a second: 1 frame a second. Decode 6 ms x 0.3 MP = 1.8 ms, inference 40 ms on the processor or 8 ms on an accelerator.
        result = capacity.estimate([row(count=2, ai=True)], 0, 4)
        self.assertAlmostEqual(result["metrics"]["cores_ai_processor"]["value"], 1.0 * (0.0018 + 0.040), places=4)
        self.assertAlmostEqual(result["metrics"]["cores_ai_accelerated"]["value"], 1.0 * (0.0018 + 0.008), places=4)
        self.assertEqual(result["ai_cameras"], 2)
        # Two viewers with two tiles each of H.265 cameras: 4 streams, half converted, each 1080p 15 fps = 0.5 core.
        converting = capacity.estimate([row(codec="h265")], viewers=2, tiles_per_viewer=2)
        self.assertEqual(converting["metrics"]["cores_conversion"]["value"], 1.0)
        # A camera with its own analysis rate.
        faster = capacity.estimate([row(count=1, ai=True, analysis_fps=2)], 0, 4)
        self.assertAlmostEqual(faster["metrics"]["cores_ai_processor"]["value"], 2 * (0.0018 + 0.040), places=4)

    def test_entered_assumptions_change_the_result_and_are_marked(self):
        result = capacity.estimate([row()], 0, 4, {"headroom": 1.5, "overhead": 1.0})
        self.assertAlmostEqual(result["metrics"]["disk_recommended"]["value"], 13_271_040_000_000 * 1.5, delta=1)
        by_key = {item["key"]: item for item in result["assumptions"]}
        self.assertEqual((by_key["headroom"]["source"], by_key["overhead"]["source"], by_key["live_share"]["source"]), ("entered", "entered", "default"))

    def test_every_result_lists_its_assumptions_and_the_estimate_says_what_it_is(self):
        result = capacity.estimate([row(ai=True, codec="h265", recording="motion")], viewers=3, tiles_per_viewer=4)
        keys = {item["key"] for item in result["assumptions"]}
        self.assertEqual(keys, set(capacity.DEFAULTS))
        for name, item in result["metrics"].items():
            self.assertTrue(item["basis"], name)
            self.assertLessEqual(set(item["assumes"]), keys, name)
        self.assertEqual(set(result["metrics"]["disk_recommended"]["assumes"]), {"overhead", "headroom"})
        self.assertIn("motion_share", result["metrics"]["write_average"]["assumes"])
        self.assertIn("not a certification", result["disclaimer"])
        self.assertTrue(all(item["used"] for item in result["assumptions"] if item["key"] in {"overhead", "headroom", "motion_share", "inference_ms_cpu", "transcode_cores_1080p15"}))


class CapacityApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in KEYS}
        os.environ.pop("NVR_STORAGE_VOLUMES", None)
        (root / "recordings").mkdir()
        os.environ.update({"NVR_DB_PATH": str(root / "data" / "nvr.sqlite3"), "NVR_CREDENTIAL_KEY_PATH": str(root / "secrets" / "credentials.key"), "NVR_RECORDINGS_PATH": str(root / "recordings")})
        context = TestClient(create_app(start_workers=False))
        self.client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.assertEqual(self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 201)

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def add_camera(self, name: str, mode: str = "continuous", width: int | None = 1920, height: int | None = 1080, codec: str | None = "h264", footage_seconds: int = 0) -> str:
        camera = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,video_codec,video_width,video_height) VALUES(?,?,?,?,?,?,?)",
                       (camera, name, encrypt("rtsp://camera.invalid/main"), mode, codec, width, height))
            start = datetime.now(timezone.utc) - timedelta(hours=10)
            for index in range(footage_seconds // 1200):
                begins = start + timedelta(seconds=index * 1200)
                db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                           (camera, f"{camera}/{index}.mp4", begins.isoformat(), (begins + timedelta(seconds=1200)).isoformat(), 600_000_000))  # 4 000 kbps while recording
        return camera

    def test_the_inventory_uses_measured_bitrates_and_the_estimate_labels_what_is_assumed(self):
        self.add_camera("Gate", mode="continuous", footage_seconds=3600)  # An hour of footage: enough to call the bitrate measured
        self.add_camera("Yard", mode="motion", width=None, height=None, codec=None, footage_seconds=0)  # Nothing recorded yet, nothing probed
        rows = self.client.get("/api/admin/capacity/inventory").json()["rows"]
        gate, yard = {item["name"]: item for item in rows}["Gate"], {item["name"]: item for item in rows}["Yard"]
        self.assertEqual((gate["bitrate_kbps"], gate["bitrate_source"], gate["codec"], gate["width"]), (4000.0, "measured", "h264", 1920))
        self.assertEqual((yard["bitrate_kbps"], yard["bitrate_source"], yard["codec"], yard["width"]), (None, "assumed", None, None))
        self.assertEqual(gate["retention_days"], 30)

        estimate = self.client.post("/api/admin/capacity/estimate", json={"rows": rows, "viewers": 1, "tiles_per_viewer": 4})
        self.assertEqual(estimate.status_code, 200, estimate.text)
        lines = {item["name"]: item for item in estimate.json()["lines"]}
        self.assertEqual((lines["Gate"]["bitrate_source"], lines["Gate"]["bitrate_kbps"], lines["Gate"]["assumed"][:2]), ("measured", 4000.0, ["frame rate (15)"]))
        self.assertEqual(lines["Yard"]["bitrate_source"], "assumed")
        self.assertIn("picture size (1920 x 1080)", lines["Yard"]["assumed"])
        self.assertIn("codec (H.264)", lines["Yard"]["assumed"])
        self.assertTrue(any(text.startswith("bitrate (") for text in lines["Yard"]["assumed"]))
        self.assertIn("not a certification", estimate.json()["disclaimer"])

    def test_a_motion_camera_with_footage_uses_its_measured_share_of_time(self):
        self.add_camera("Yard", mode="motion", footage_seconds=3600)
        row_ = self.client.get("/api/admin/capacity/inventory").json()["rows"][0]
        self.assertEqual(row_["recorded_share"], round(3600 / (48 * 3600), 4))
        line = self.client.post("/api/admin/capacity/estimate", json={"rows": [row_]}).json()["lines"][0]
        self.assertEqual(line["duty"], round(3600 / (48 * 3600), 4))
        self.assertFalse(any("share of the day" in text for text in line["assumed"]))  # Measured, not assumed

    def test_the_estimate_is_validated(self):
        base = {"name": "A", "count": 1}
        for bad in ({"rows": []}, {"rows": [{**base, "count": 0}]}, {"rows": [{**base, "bitrate_kbps": 1}]}, {"rows": [{**base, "codec": "av1"}]}, {"rows": [{**base, "retention_days": 0}]},
                    {"rows": [base], "viewers": -1}, {"rows": [base], "assumptions": {"nonsense": 1}}, {"rows": [base], "assumptions": {"headroom": 0.5}}):
            self.assertEqual(self.client.post("/api/admin/capacity/estimate", json=bad).status_code, 422, bad)
        ok = self.client.post("/api/admin/capacity/estimate", json={"rows": [base], "assumptions": {"headroom": 1.5}})
        self.assertEqual(ok.status_code, 200, ok.text)
        listing = self.client.get("/api/admin/capacity/assumptions").json()["assumptions"]
        self.assertEqual({item["key"] for item in listing}, set(capacity.DEFAULTS))

    def test_only_administrators_may_plan(self):
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('watcher',?,'operator')", (hash_password(TEST_PASSWORD),))
        with TestClient(self.client.app) as operator:
            self.assertEqual(operator.post("/api/auth/login", json={"username": "watcher", "password": TEST_PASSWORD}).status_code, 200)
            self.assertEqual(operator.get("/api/admin/capacity/inventory").status_code, 403)
            self.assertEqual(operator.post("/api/admin/capacity/estimate", json={"rows": [{"name": "A"}]}).status_code, 403)
            self.assertEqual(operator.get("/api/admin/capacity/assumptions").status_code, 403)


if __name__ == "__main__":
    unittest.main()
