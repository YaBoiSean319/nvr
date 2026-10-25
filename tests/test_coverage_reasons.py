"""Gap reasons, the weekly coverage report and the nightly self-test (#117)."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import coverage, self_test
from app.application import create_app
from app.credentials import encrypt
from app.recording import RecordingManager
from app.store import connect
from tests.test_support import TEST_PASSWORD

NOW = datetime.now(timezone.utc)


def ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


class CoverageCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.recordings = root / "recordings"
        self.recordings.mkdir()
        self.environment = {
            "NVR_DB_PATH": str(root / "state.sqlite3"),
            "NVR_RECORDINGS_PATH": str(self.recordings),
            "NVR_CREDENTIAL_KEY_PATH": str(root / "credentials.key"),
            "NVR_BACKUP_PATH": str(root / "backups"),
            "NVR_CONTROL_DIR": str(root / "run"),
        }
        self.previous = {key: os.environ.get(key) for key in (*self.environment, "NVR_STORAGE_VOLUMES", "NVR_BACKUP_PASSPHRASE")}
        os.environ.update(self.environment)
        for key in ("NVR_STORAGE_VOLUMES", "NVR_BACKUP_PASSPHRASE"):
            os.environ.pop(key, None)
        self.context = TestClient(create_app(start_workers=False))
        self.client = self.context.__enter__()
        self.assertEqual(self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 201)
        self.camera_id = self.add_camera("Loading dock")

    def tearDown(self):
        self.context.__exit__(None, None, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def add_camera(self, name: str, stream: str = "rtsp://camera.invalid/main") -> str:
        camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,recording_enabled,recording_status) VALUES(?,?,?,'continuous',1,'connecting')",
                       (camera_id, name, encrypt(stream)))
        return camera_id

    def latest_interval(self, camera_id: str | None = None) -> dict:
        with connect() as db:
            return dict(db.execute("SELECT * FROM coverage_intervals WHERE camera_id=? ORDER BY id DESC LIMIT 1", (camera_id or self.camera_id,)).fetchone())

    def run_recorder_until(self, camera_id: str, condition, timeout: float = 20) -> None:
        """Run the real continuous recorder for one camera until ``condition()`` holds."""
        async def drive():
            manager = RecordingManager()
            task = asyncio.create_task(manager._run_continuous(camera_id))
            try:
                deadline = asyncio.get_running_loop().time() + timeout
                while not condition():
                    self.assertLess(asyncio.get_running_loop().time(), deadline, "the recorder never reached the expected state")
                    await asyncio.sleep(0.2)
            finally:
                manager.stopping = True
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(drive())

    def fault(self, camera_id: str) -> str | None:
        with connect() as db:
            return db.execute("SELECT stream_fault FROM cameras WHERE id=?", (camera_id,)).fetchone()[0]


class GapReasonTests(CoverageCase):
    def sync(self, camera_id: str | None = None) -> dict:
        RecordingManager()._sync_coverage(camera_id or self.camera_id)
        return self.latest_interval(camera_id)

    def test_an_unreachable_camera_is_offline(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()  # Nothing listens here.
        camera = self.add_camera("Gate", f"rtsp://127.0.0.1:{port}/stream")
        self.run_recorder_until(camera, lambda: self.fault(camera) is not None)
        self.assertEqual(self.sync(camera)["reason_code"], "camera_offline")

    def test_a_camera_that_answers_but_streams_nothing_usable_is_a_stream_error(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)

        def refuse():
            while True:
                try:
                    connection, _ = listener.accept()
                except OSError:
                    return
                connection.sendall(b"not RTSP\r\n\r\n")
                connection.close()
        threading.Thread(target=refuse, daemon=True).start()
        camera = self.add_camera("Yard", f"rtsp://127.0.0.1:{listener.getsockname()[1]}/stream")
        self.run_recorder_until(camera, lambda: self.fault(camera) is not None)
        self.assertEqual(self.sync(camera)["reason_code"], "stream_error")

    def test_an_unwritable_recording_disk_is_disk_unavailable(self):
        self.recordings.rmdir()
        self.recordings.write_text("not a directory")  # The camera's recordings folder cannot be created.
        self.run_recorder_until(self.camera_id, lambda: self.fault(self.camera_id) is not None)
        self.assertEqual(self.sync()["reason_code"], "disk_unavailable")

    def test_a_volume_with_a_storage_incident_is_disk_unavailable(self):
        from app.incidents import sync_storage_incidents

        with connect() as db:
            sync_storage_incidents(db, [{"id": "default", "state": "pressure", "cameras": [{"id": self.camera_id, "name": "Loading dock"}]}])
        self.assertEqual(self.sync()["reason_code"], "disk_unavailable")

    def test_schedules_disabled_cameras_restarts_and_unknowns_have_their_reasons(self):
        self.assertEqual(self.sync()["reason_code"], "unknown")  # No video, and nothing says why.
        with connect() as db:
            db.execute("INSERT INTO recording_policy_overrides(scope_type,scope_id,timezone_name,schedule_json) VALUES('camera',?,'UTC','[]')", (self.camera_id,))
        self.assertEqual(self.sync()["reason_code"], "outside_schedule")
        with connect() as db:
            db.execute("UPDATE cameras SET recording_enabled=0 WHERE id=?", (self.camera_id,))
        self.assertEqual(self.sync()["reason_code"], "recording_disabled")

        with connect() as db:
            db.execute("DELETE FROM recording_policy_overrides")
            db.execute("UPDATE cameras SET recording_enabled=1, last_video_at=?, last_footage_write_at=? WHERE id=?", (ago(seconds=1), ago(seconds=1), self.camera_id))
        self.assertIsNone(self.sync()["reason_code"])  # Covered.
        with connect() as db:
            db.execute("UPDATE coverage_intervals SET checked_at=? WHERE ends_at IS NULL", (ago(minutes=10),))

        async def restart():
            manager = RecordingManager()
            await manager.start()
            await manager.stop()
        asyncio.run(restart())
        with connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM coverage_intervals WHERE camera_id=? ORDER BY id DESC LIMIT 2", (self.camera_id,))]
        restart, covered = rows
        self.assertEqual((restart["coverage_kind"], restart["reason_code"]), ("failure", "worker_restart"))
        self.assertEqual(covered["ends_at"], restart["starts_at"])
        self.assertIsNotNone(restart["ends_at"])

    def test_coverage_responses_keep_their_fields_and_add_the_reason_code(self):
        self.sync()
        start, end = NOW - timedelta(minutes=5), NOW + timedelta(minutes=5)
        timeline = self.client.post("/api/playback/timeline", json={"camera_ids": [self.camera_id], "start": start.isoformat(), "end": end.isoformat()}).json()
        camera = timeline["cameras"][0]
        self.assertEqual(set(camera["gaps"][0]), {"starts_at", "ends_at", "state", "reason", "reason_code"})
        self.assertEqual(set(camera["coverage"][0]), {"coverage_kind", "reason", "reason_code", "starts_at", "ends_at"})
        self.assertEqual(camera["gaps"][-1]["reason_code"], "unknown")
        intervals = self.client.get(f"/api/cameras/{self.camera_id}/coverage").json()["intervals"]
        self.assertEqual(set(intervals[0]), {"coverage_kind", "reason", "reason_code", "starts_at", "ends_at"})

    def test_every_reason_shows_in_the_timeline_gap_it_explains(self):
        codes = ["camera_offline", "stream_error", "disk_unavailable", "outside_schedule", "recording_disabled", "worker_restart", "no_motion", "unknown"]
        start = NOW - timedelta(hours=1)
        at = lambda minutes: (start + timedelta(minutes=minutes)).isoformat()
        with connect() as db:
            for index, code in enumerate(codes):
                kind = "expected_idle" if code in coverage.EXPECTED else "failure"
                db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,reason_code,starts_at,ends_at) VALUES(?,?,?,?,?,?)",
                           (self.camera_id, kind, f"fixture {code}", code, at(index * 5), at(index * 5 + 5)))
        timeline = self.client.post("/api/playback/timeline", json={"camera_ids": [self.camera_id], "start": at(0), "end": at(40)}).json()
        gaps = timeline["cameras"][0]["gaps"]
        self.assertEqual([(gap["reason_code"], gap["reason"]) for gap in gaps], [(code, f"fixture {code}") for code in codes])

    def test_a_stream_fault_ends_with_the_stream_and_is_not_carried_into_other_gaps(self):
        with connect() as db:
            db.execute("UPDATE cameras SET stream_fault='camera_offline', recording_status='reconnecting' WHERE id=?", (self.camera_id,))
        self.assertEqual(self.sync()["reason_code"], "camera_offline")
        with connect() as db:  # Video is flowing again: the old fault no longer explains anything.
            db.execute("UPDATE cameras SET last_video_at=?, recording_status='connecting' WHERE id=?", (ago(seconds=1), self.camera_id))
        self.assertEqual(self.fault(self.camera_id), "camera_offline")
        self.sync()
        self.assertIsNone(self.fault(self.camera_id))
        RecordingManager()._set_status(self.camera_id, "reconnecting", "Camera stream interrupted; retrying", fault="stream_error")
        RecordingManager()._set_status(self.camera_id, "unavailable", "FFmpeg is not installed")  # No fault named: unknown, not the last one.
        self.assertIsNone(self.fault(self.camera_id))

    def test_downtime_while_nothing_was_expected_is_not_an_outage(self):
        with connect() as db:
            db.execute("UPDATE cameras SET recording_enabled=0 WHERE id=?", (self.camera_id,))
        self.assertEqual(self.sync()["reason_code"], "recording_disabled")
        with connect() as db:
            db.execute("UPDATE coverage_intervals SET checked_at=? WHERE ends_at IS NULL", (ago(hours=3),))
        RecordingManager._mark_restart_gaps()
        with connect() as db:
            rows = db.execute("SELECT reason_code,ends_at FROM coverage_intervals WHERE camera_id=?", (self.camera_id,)).fetchall()
        self.assertEqual([(row["reason_code"], row["ends_at"]) for row in rows], [("recording_disabled", None)])


class CoverageReportTests(CoverageCase):
    def test_the_report_matches_the_coverage_api_for_the_same_range(self):
        start = (NOW - timedelta(hours=6)).replace(microsecond=0)
        end = start + timedelta(hours=4)
        at = lambda minutes: (start + timedelta(minutes=minutes)).isoformat()
        with connect() as db:
            db.execute("UPDATE cameras SET created_at=?", ((start - timedelta(days=1)).isoformat(),))
            for offset in (0, 30, 60, 150, 200):
                db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,1000)",
                           (self.camera_id, f"{self.camera_id}/s{offset}.mp4", at(offset), at(offset + 20)))
            for kind, code, since, until in (("covered", None, -10, 80), ("failure", "camera_offline", 80, 140), ("expected_idle", "outside_schedule", 140, 150),
                                             ("covered", None, 150, 170), ("failure", "disk_unavailable", 170, 200), ("covered", None, 200, 300)):
                db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,reason_code,starts_at,ends_at) VALUES(?,?,?,?,?,?)",
                           (self.camera_id, kind, "fixture", code, at(since), at(until)))
        report = self.client.get("/api/coverage/report", params={"start": start.isoformat(), "end": end.isoformat()}).json()
        timeline = self.client.post("/api/playback/timeline", json={"camera_ids": [self.camera_id], "start": start.isoformat(), "end": end.isoformat()}).json()
        gaps = timeline["cameras"][0]["gaps"]
        seconds = lambda gap: (datetime.fromisoformat(gap["ends_at"]) - datetime.fromisoformat(gap["starts_at"])).total_seconds()
        outages = [gap for gap in gaps if gap["reason_code"] not in coverage.EXPECTED]
        camera = next(item for item in report["cameras"] if item["camera_id"] == self.camera_id)
        self.assertEqual(camera["missed_seconds"], round(sum(map(seconds, outages)), 1))
        self.assertEqual(camera["expected_seconds"], 4 * 3600 - sum(seconds(gap) for gap in gaps if gap["reason_code"] in coverage.EXPECTED))
        self.assertEqual(camera["missed_by_reason"], {"camera_offline": 3600.0, "unknown": 2400.0, "disk_unavailable": 1800.0})
        self.assertEqual([gap["reason_code"] for gap in camera["largest_gaps"]][:2], ["camera_offline", "disk_unavailable"])
        for gap in camera["largest_gaps"]:
            self.assertIn({key: gap[key] for key in ("starts_at", "ends_at", "state", "reason", "reason_code")}, gaps)
        self.assertEqual(camera["coverage_percent"], round(100 * (camera["expected_seconds"] - camera["missed_seconds"]) / camera["expected_seconds"], 2))

    def test_a_report_counts_only_time_that_has_happened_since_the_camera_was_added(self):
        with connect() as db:
            db.execute("UPDATE cameras SET created_at=?", (ago(hours=2),))
        report = self.client.get("/api/coverage/report", params={"start": ago(days=3), "end": (NOW + timedelta(days=3)).isoformat()}).json()
        camera = report["cameras"][0]
        self.assertAlmostEqual(camera["expected_seconds"], 2 * 3600, delta=60)  # Not a week.
        self.assertLessEqual(report["end"], datetime.now(timezone.utc).isoformat())

    def test_last_weeks_report_is_stored_once_and_sent_to_subscribed_webhooks(self):
        from app.webhooks import create_endpoint

        create_endpoint("Coverage mail", "http://10.0.0.9/hook", ["coverage.weekly_report"], True, 1)
        self.assertIsNotNone(coverage.publish_weekly_report())
        self.assertIsNone(coverage.publish_weekly_report())
        stored = self.client.get("/api/coverage/reports").json()["reports"]
        self.assertEqual(len(stored), 1)
        with connect() as db:
            delivery = db.execute("SELECT event_type,payload_json FROM webhook_deliveries").fetchone()
        self.assertEqual(delivery["event_type"], "coverage.weekly_report")
        self.assertEqual(json.loads(delivery["payload_json"])["report"]["start"], stored[0]["period_start"])


class SelfTestTests(CoverageCase):
    def incident(self, key: str) -> dict | None:
        with connect() as db:
            row = db.execute("SELECT state,fault_active,summary FROM incidents WHERE incident_key=?", (f"self_test:{key}",)).fetchone()
        return dict(row) if row else None

    def test_a_silent_camera_raises_an_incident_that_recovers_once_it_records(self):
        with connect() as db:  # Expected to record all day, wrote nothing.
            db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,reason_code,starts_at) VALUES(?,?,?,?,?)",
                       (self.camera_id, "failure", "fixture", "camera_offline", ago(days=2)))
            quiet = self.add_camera("Storeroom")
            db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,reason_code,starts_at) VALUES(?,?,?,?,?)",
                       (quiet, "expected_idle", "fixture", "outside_schedule", ago(days=2)))
        result = self.client.post("/api/self-test/run").json()["latest"]
        self.assertFalse(result["passed"])
        self.assertTrue(self.incident(f"camera:{self.camera_id}")["fault_active"])
        self.assertIsNone(self.incident(f"camera:{quiet}"))  # Not expected to record: not checked.
        with connect() as db:
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,1)",
                       (self.camera_id, "x/1.mp4", ago(minutes=2), ago(minutes=1)))
        self.assertTrue(self.client.post("/api/self-test/run").json()["latest"]["passed"])
        self.assertEqual(self.incident(f"camera:{self.camera_id}")["state"], "recovered")

    def test_an_unwritable_volume_raises_an_incident_that_recovers_when_fixed(self):
        os.environ["NVR_STORAGE_VOLUMES"] = json.dumps({"default": "", "archive": "archive"})
        self.client.post("/api/self-test/run")
        self.assertTrue(self.incident("volume:archive")["fault_active"])
        self.assertIsNone(self.incident("volume:default"))
        (self.recordings / "archive").mkdir()
        self.client.post("/api/self-test/run")
        self.assertEqual(self.incident("volume:archive")["state"], "recovered")

    def test_a_stale_backup_raises_an_incident_that_recovers_after_a_fresh_one(self):
        self.client.post("/api/self-test/run")
        self.assertIsNone(self.incident("backup"))  # Backups not configured: reported, not raised.
        os.environ["NVR_BACKUP_PASSPHRASE"] = "a long backup passphrase 1"
        result = self.client.post("/api/self-test/run").json()["latest"]
        self.assertIsNone(next(item for item in result["results"] if item["check"] == "backup")["passed"])  # New: the first runs tonight.
        self.assertIsNone(self.incident("backup"))
        with connect() as db:
            db.execute("INSERT INTO backup_runs(id,kind,status,created_at,finished_at) VALUES('old','scheduled','succeeded',?,?)", (ago(hours=31), ago(hours=30)))
        result = self.client.post("/api/self-test/run").json()["latest"]
        self.assertIn("30 hours old", next(item for item in result["results"] if item["check"] == "backup")["detail"])
        self.assertTrue(self.incident("backup")["fault_active"])
        with connect() as db:
            db.execute("INSERT INTO backup_runs(id,kind,status,created_at,finished_at) VALUES('new','manual','succeeded',?,?)", (ago(minutes=2), ago(minutes=1)))
        self.client.post("/api/self-test/run")
        self.assertEqual(self.incident("backup")["state"], "recovered")

    def test_a_run_that_crashes_is_retried_rather_than_counted_as_tonights(self):
        from unittest import mock

        with mock.patch.object(self_test, "_check_volumes", side_effect=OSError("boom")), self.assertRaises(OSError):
            self_test.run("scheduled")
        self.assertTrue(self_test.due())
        self_test.run("scheduled")
        self.assertFalse(self_test.due())

    def test_the_nightly_run_happens_once_a_night_and_shows_in_system_status(self):
        self.assertTrue(self_test.due())
        self_test.SelfTestManager.process_once()
        self.assertFalse(self_test.due())
        status = self.client.get("/api/system/status").json()
        self.assertEqual(status["self_test"]["kind"], "scheduled")
        self.assertIn("results", status["self_test"])


if __name__ == "__main__":
    unittest.main()
