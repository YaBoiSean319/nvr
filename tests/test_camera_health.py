"""Camera health (#137): what cameras report, what is measured, firmware outliers, the recorder's counters, access, and a fleet of 200 without media."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import subprocess
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import camera_health, metrics
from app.credentials import encrypt
from app.store import connect
from tests.fixtures.onvif_camera import OnvifCamera
from tests.test_access_grants import GrantCase

USER, PASSWORD = "operator", "S3cret!pass"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


class HealthCase(GrantCase):
    def tearDown(self):
        for camera in getattr(self, "devices", []):
            camera.close()
        super().tearDown()

    def device(self, camera_id: str, **options) -> OnvifCamera:
        camera = OnvifCamera(username=USER, password=PASSWORD, **options)
        self.devices = [*getattr(self, "devices", []), camera]
        with connect() as db:
            db.execute("UPDATE cameras SET device_service_url=?,motion_credentials_ciphertext=? WHERE id=?",
                       (camera.url, encrypt(json.dumps({"username": USER, "password": PASSWORD})), camera_id))
        return camera

    def sample(self, now: datetime | None = None) -> int:
        return asyncio.run(camera_health.sample_once(now or NOW))

    def fleet(self, client=None) -> dict:
        response = (client or self.admin).get("/api/camera-health")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def row(self, report: dict, camera_id: str) -> dict:
        return next(row for row in report["cameras"] if row["camera_id"] == camera_id)


class ReportTests(HealthCase):
    def test_a_camera_reports_its_make_model_firmware_and_settings_and_a_silent_one_says_not_reported(self):
        self.device(self.d1, manufacturer="Hanwha Techwin", model="XNV-6120R", firmware="2.00.00")
        self.assertGreaterEqual(self.sample(), 1)
        report = self.fleet()
        talking, silent = self.row(report, self.d1), self.row(report, self.d2)
        self.assertEqual((talking["manufacturer"], talking["model"], talking["firmware"]), ("Hanwha Techwin", "XNV-6120R", "2.00.00"))
        self.assertEqual(talking["configured"]["codec"], "h264")
        self.assertGreater(talking["configured"]["bitrate_kbps"], 0)
        self.assertGreater(talking["configured"]["fps"], 0)
        self.assertIsNotNone(talking["onvif_contact_at"])
        self.assertIsNone(talking["onvif_error"])
        # Nothing the camera did not say is guessed.
        for key in ("manufacturer", "model", "firmware", "uptime_seconds", "onvif_contact_at"):
            self.assertIsNone(silent[key], key)
        self.assertEqual(silent["configured"], {"codec": None, "width": None, "height": None, "fps": None, "bitrate_kbps": None})
        self.assertEqual(silent["measured"]["bitrate_kbps"], None)
        rows = list(csv.DictReader(io.StringIO(self.admin.get("/api/camera-health/export.csv").text)))
        by_name = {row["camera"]: row for row in rows}
        self.assertEqual(by_name["Dock 2"]["firmware"], "Not reported")
        self.assertEqual(by_name["Dock 2"]["last_onvif_contact"], "Not reported")
        self.assertEqual(by_name["Dock 1"]["firmware"], "2.00.00")

    def test_a_camera_that_cannot_be_reached_keeps_what_it_last_said_and_shows_why(self):
        camera = self.device(self.d1, firmware="1.0.0")
        self.sample()
        first = self.row(self.fleet(), self.d1)["onvif_contact_at"]
        camera.close()
        self.sample(NOW + timedelta(hours=1))
        later = self.row(self.fleet(), self.d1)
        self.assertEqual(later["firmware"], "1.0.0")  # Not forgotten
        self.assertEqual(later["onvif_contact_at"], first)  # The last contact that worked
        self.assertTrue(later["onvif_error"])

    def test_the_reason_a_camera_could_not_be_reached_is_for_administrators_only(self):
        camera = self.device(self.d1)
        camera.close()
        self.sample()
        self.assertTrue(self.row(self.fleet(), self.d1)["onvif_error"])
        limited = self.row(self.fleet(self.limited), self.d1)
        self.assertIsNone(limited["onvif_error"])
        self.assertTrue(limited["onvif_failed"])  # They are told it failed, not what the camera said

    def test_one_camera_that_cannot_be_sampled_does_not_stop_the_others(self):
        self.device(self.d1, firmware="1.0.0")
        self.device(self.d2, firmware="2.0.0")
        real = camera_health.read_onvif

        async def flaky(row):
            if row["id"] == self.d1:
                raise RuntimeError("unexpected")
            return await real(row)

        with patch("app.camera_health.read_onvif", flaky):
            self.sample()
        report = self.fleet()
        self.assertEqual(self.row(report, self.d2)["firmware"], "2.0.0")
        with connect() as db:
            self.assertIsNotNone(db.execute("SELECT onvif_checked_at FROM camera_health WHERE camera_id=?", (self.d1,)).fetchone()[0])  # Not retried at once

    def test_values_are_refreshed_only_when_stale(self):
        camera = self.device(self.d1)
        self.sample()
        asked = len(camera.requests)
        self.assertEqual(self.sample(NOW + timedelta(seconds=30)), 0)
        self.assertEqual(len(camera.requests), asked)
        self.assertGreaterEqual(self.sample(NOW + timedelta(seconds=camera_health.SAMPLE_SECONDS + 60)), 1)
        self.assertGreater(len(camera.requests), asked)


class FirmwareTests(HealthCase):
    def test_a_camera_on_older_firmware_than_others_of_its_model_is_flagged(self):
        self.device(self.d1, model="XNV-6120R", firmware="2.10.0")
        self.device(self.d2, model="XNV-6120R", firmware="2.9.1")  # Older by number, though it sorts after as text
        self.device(self.y1, model="PNM-9000", firmware="1.0.0")   # The only one of its model
        self.sample()
        report = self.fleet()
        self.assertEqual([self.row(report, camera)["firmware_status"] for camera in (self.d1, self.d2, self.y1)], ["current", "outdated", "uniform"])
        self.assertEqual(self.row(report, self.d2)["newest_firmware"], "2.10.0")
        self.assertEqual(report["outdated"], 1)
        [xnv, pnm] = report["models"]
        self.assertEqual((xnv["model"], xnv["newest"], xnv["outdated"], [item["version"] for item in xnv["firmwares"]]), ("XNV-6120R", "2.10.0", 1, ["2.10.0", "2.9.1"]))
        self.assertEqual(pnm["outdated"], 0)

    def test_versions_compare_in_natural_order(self):
        order = sorted(["2.9.1", "2.10.0", "2.10", "2.9.10", "1.2 build 5"], key=camera_health.version_key)
        self.assertEqual(order, ["1.2 build 5", "2.9.1", "2.9.10", "2.10", "2.10.0"])
        self.assertLess(camera_health.version_key("1.0-rc1"), camera_health.version_key("1.0"))  # A release is newer than its pre-release
        self.assertLess(camera_health.version_key("1.0"), camera_health.version_key("1.0.1"))
        self.assertEqual(camera_health.version_key("V2.1"), camera_health.version_key("2.1"))
        self.assertEqual(camera_health.version_key("2.0A"), camera_health.version_key("2.0a"))

    def test_spellings_of_one_version_are_not_called_outdated(self):
        self.device(self.d1, model="XNV-6120R", firmware="v2.1")
        self.device(self.d2, model="XNV-6120R", firmware="2.1")
        self.sample()
        self.assertEqual([self.row(self.fleet(), camera)["firmware_status"] for camera in (self.d1, self.d2)], ["uniform", "uniform"])

    def test_firmware_is_compared_across_the_whole_fleet_but_a_reader_is_not_told_what_a_hidden_camera_runs(self):
        self.device(self.d1, model="XNV-6120R", firmware="2.9.1")
        self.device(self.y1, model="XNV-6120R", firmware="3.0.0")  # Hidden from the limited operator
        self.sample()
        everything = self.fleet()
        self.assertEqual((self.row(everything, self.d1)["firmware_status"], self.row(everything, self.d1)["newest_firmware"]), ("outdated", "3.0.0"))
        limited = self.fleet(self.limited)
        self.assertEqual(self.row(limited, self.d1)["firmware_status"], "outdated")  # Judged against the fleet, so still flagged
        self.assertIsNone(self.row(limited, self.d1)["newest_firmware"])  # but the version of a camera they cannot see is not named
        self.assertNotIn("3.0.0", json.dumps(limited))
        self.assertEqual([item["cameras"] for item in limited["models"]], [1])
        self.assertEqual({row["camera_id"] for row in limited["cameras"]}, {self.d1, self.d2})


class CounterTests(HealthCase):
    def test_reconnects_follow_the_recorders_state_and_dropped_frames_its_ffmpeg_report(self):
        with connect() as db:
            for status in ("connecting", "recording", "reconnecting", "reconnecting", "recording", "reconnecting"):  # Two breaks (the repeat is the same break), then a third
                db.execute("UPDATE cameras SET recording_status=? WHERE id=?", (status, self.d1))
            counters = dict(db.execute("SELECT name,value FROM metric_counters WHERE camera_id=?", (self.d1,)).fetchall())
        self.assertEqual(counters["reconnects"], 2)
        # FFmpeg's progress report: a running total of drop_frames per process, counted as it grows.
        counter = camera_health.ProgressCounter(self.d1)
        for line in ("frame=100", "drop_frames=0", "progress=continue", "drop_frames=3", "drop_frames=3", "drop_frames=7", "progress=end"):
            counter.feed(line)
        counter = camera_health.ProgressCounter(self.d1)  # The recorder restarted FFmpeg: its total starts again
        counter.feed("drop_frames=2")
        row = self.row(self.fleet(), self.d1)
        self.assertEqual((row["reconnects"], row["dropped_frames"]), (2, 9))
        with connect() as db:
            self.assertEqual(dict(db.execute("SELECT name,value FROM metric_counters WHERE camera_id=?", (self.d1,)).fetchall())[camera_health.FRAMES_DROPPED], 9)
        self.assertEqual(self.row(self.fleet(), self.d2)["reconnects"], None)  # The recorder has not counted this camera at all

    def test_the_counters_are_the_ones_the_metrics_page_reports(self):
        with connect() as db:
            db.execute("UPDATE cameras SET recording_status='reconnecting' WHERE id=?", (self.d1,))
            metrics.bump(db, camera_health.FRAMES_DROPPED, self.d1, 4)
            metrics.bump(db, metrics.SEGMENTS_DROPPED, self.d1, 1)
        row = self.row(self.fleet(), self.d1)
        self.assertEqual((row["reconnects"], row["dropped_frames"], row["dropped_segments"]), (1, 4, 1))

    def test_the_progress_report_of_a_real_process_is_read_to_its_end(self):
        async def run() -> None:
            stream = asyncio.StreamReader()
            stream.feed_data(b"drop_frames=1\nprogress=continue\ndrop_frames=5\nprogress=end\n")
            stream.feed_eof()
            await camera_health.watch_progress(stream, self.d1)
        asyncio.run(run())
        self.assertEqual(self.row(self.fleet(), self.d1)["dropped_frames"], 5)

    def test_uptime_runs_from_when_recording_began_and_ends_with_a_break(self):
        with connect() as db:
            db.execute("UPDATE cameras SET recording_status='recording' WHERE id=?", (self.d1,))
            first = db.execute("SELECT stream_up_since FROM camera_health WHERE camera_id=?", (self.d1,)).fetchone()[0]
            db.execute("UPDATE cameras SET recording_status='buffering' WHERE id=?", (self.d1,))  # Still up: motion mode
            self.assertEqual(db.execute("SELECT stream_up_since FROM camera_health WHERE camera_id=?", (self.d1,)).fetchone()[0], first)
            db.execute("UPDATE camera_health SET stream_up_since=? WHERE camera_id=?", ((NOW - timedelta(hours=2)).isoformat(), self.d1))
        row = self.row(self.fleet(), self.d1)
        self.assertGreaterEqual(row["uptime_seconds"], 7200)
        with connect() as db:
            db.execute("UPDATE cameras SET recording_status='reconnecting' WHERE id=?", (self.d1,))
        self.assertIsNone(self.row(self.fleet(), self.d1)["uptime_seconds"])


class MeasureTests(HealthCase):
    def test_the_measured_bitrate_and_frame_rate_come_from_the_recordings(self):
        import os
        from app.storage import recording_file_path
        relative = f"{self.d1}/health.mp4"
        path = recording_file_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-t", "6", "-c:v", "libx264", "-y", str(path)], check=True)
        with connect() as db:
            db.execute("UPDATE cameras SET recording_enabled=1 WHERE id=?", (self.d1,))
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                       (self.d1, relative, (NOW - timedelta(seconds=70)).isoformat(), (NOW - timedelta(seconds=10)).isoformat(), 600_000))
        self.sample()
        measured = self.row(self.fleet(), self.d1)["measured"]
        self.assertEqual(measured["bitrate_kbps"], 80.0)  # 600 kB over 60 s
        self.assertEqual(measured["fps"], 10.0)
        self.assertIsNotNone(measured["at"])
        self.assertTrue(os.path.exists(path))

    def test_a_camera_with_no_recordings_has_nothing_measured(self):
        with connect() as db:
            db.execute("UPDATE cameras SET recording_enabled=1 WHERE id=?", (self.d1,))
        self.sample()
        measured = self.row(self.fleet(), self.d1)["measured"]
        self.assertEqual((measured["bitrate_kbps"], measured["fps"]), (None, None))


class ScaleAndAccessTests(HealthCase):
    def test_two_hundred_cameras_are_shown_without_starting_anything(self):
        with connect() as db:
            for index in range(200):
                camera = str(uuid.uuid4())
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,recording_enabled) VALUES(?,?,?,'continuous',1)", (camera, f"Bulk {index:03d}", encrypt("rtsp://camera.invalid/main")))
                db.execute("INSERT INTO camera_health(camera_id,manufacturer,model,firmware) VALUES(?,?,?,?)", (camera, "Acme", "Cam-1", f"1.{index % 7}.0"))
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("no process may be started")), patch("subprocess.run", side_effect=AssertionError("no process may be started")), \
             patch("subprocess.Popen", side_effect=AssertionError("no process may be started")):
            started = time.monotonic()
            report = self.fleet()
            export = self.admin.get("/api/camera-health/export.csv")
        self.assertGreaterEqual(len(report["cameras"]), 204)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(export.text)))), len(report["cameras"]))
        self.assertEqual(report["models"][0]["cameras"], 200)

    def test_a_person_sees_only_their_cameras_and_a_name_that_would_run_as_a_formula_is_defused(self):
        with connect() as db:
            db.execute("UPDATE cameras SET name='=cmd|x' WHERE id=?", (self.d1,))
        limited = self.fleet(self.limited)
        self.assertEqual({row["camera_id"] for row in limited["cameras"]}, {self.d1, self.d2})
        csv_text = self.limited.get("/api/camera-health/export.csv").text
        self.assertIn("'=cmd|x", csv_text)
        self.assertNotIn("Yard 1", csv_text)

    def test_it_needs_signing_in(self):
        from fastapi.testclient import TestClient
        self.assertEqual(TestClient(self.app).get("/api/camera-health").status_code, 401)


if __name__ == "__main__":
    unittest.main()
