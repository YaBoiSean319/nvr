from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.exports import ExportManager, create_export_record
from app.main import app, current_user
from app.retention import cleanup_expired_recordings, playback_is_leased
from app.store import connect, initialize


class ExportJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_STORAGE_VOLUMES")}
        self.root = Path(self.temporary.name) / "recordings"
        self.root.mkdir()
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root)
        os.environ.pop("NVR_STORAGE_VOLUMES", None)
        initialize()
        self.camera = str(uuid.uuid4())
        self.user_id = 1
        self.other_user_id = 2
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'exporter','unused','operator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(2,'second','unused','operator')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (self.camera, "Export camera"))
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        if self.ffmpeg:
            media_dir = self.root / self.camera
            media_dir.mkdir(parents=True)
            self.media = media_dir / "clip.mp4"
            subprocess.run([
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10",
                "-t", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(self.media),
            ], check=True, timeout=30)
        else:
            self.media = self.root / self.camera / "clip.mp4"
            self.media.parent.mkdir(parents=True)
            self.media.write_bytes(b"fake video")
        with connect() as db:
            self.segment_id = db.execute(
                "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                (self.camera, f"{self.camera}/clip.mp4", "2026-10-20T10:00:00+00:00", "2026-10-20T10:00:01+00:00", self.media.stat().st_size),
            ).lastrowid
        app.dependency_overrides[current_user] = lambda: {"id": self.user_id, "username": "exporter", "role": "operator"}
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        app.dependency_overrides.pop(current_user, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def _create(self, actor_id: int | None = None) -> str:
        if actor_id is not None and actor_id != self.user_id:
            app.dependency_overrides[current_user] = lambda: {"id": actor_id, "username": "second", "role": "operator"}
        response = self.client.post("/api/exports", json={
            "camera_ids": [self.camera],
            "start": "2026-10-20T09:59:59.500000Z",
            "end": "2026-10-20T10:00:00.800000Z",
        })
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["id"]

    def _wait_for_status(self, job_id: str, *terminal: str) -> dict:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/exports/{job_id}")
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            if result["status"] in terminal:
                return result
            time.sleep(0.05)
        self.fail(f"Export {job_id} did not reach {terminal}: {result}")

    def test_export_is_playable_and_metadata_lists_actual_and_missing_intervals(self):
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("FFmpeg and ffprobe are required for the playable export check")
        job_id = self._create()
        with connect() as db:
            db.execute("INSERT INTO recording_holds(id,segment_id,camera_id,created_by) VALUES(?,?,?,?)", (str(uuid.uuid4()), self.segment_id, self.camera, self.user_id))
        result = self._wait_for_status(job_id, "completed", "failed")
        self.assertEqual(result["status"], "completed", result)
        download = self.client.get(result["download_url"])
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(download.headers["content-type"], "application/zip")
        package = Path(self.temporary.name) / "result.zip"
        package.write_bytes(download.content)
        with zipfile.ZipFile(package) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            self.assertIn("camera-0.mp4", archive.namelist())
            exported_video = Path(self.temporary.name) / "camera.mp4"
            exported_video.write_bytes(archive.read("camera-0.mp4"))
        self.assertEqual(metadata["cameras"][0]["actual_intervals"], [{"start": "2026-10-20T10:00:00+00:00", "end": "2026-10-20T10:00:00.800000+00:00"}])
        self.assertEqual(metadata["cameras"][0]["missing_intervals"], [
            {"start": "2026-10-20T09:59:59.500000+00:00", "end": "2026-10-20T10:00:00+00:00"},
        ])
        probe = subprocess.run([self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(exported_video)], capture_output=True, check=True, timeout=15)
        self.assertGreater(float(json.loads(probe.stdout)["format"]["duration"]), 0)
        self.assertFalse(playback_is_leased(self.media))
        with connect() as db:
            actions = [row[0] for row in db.execute("SELECT action FROM audit_log WHERE resource_id=? ORDER BY id", (job_id,))]
            hold = db.execute("SELECT 1 FROM recording_holds WHERE segment_id=? AND released_at IS NULL", (self.segment_id,)).fetchone()
        self.assertEqual(actions, ["export_requested", "export_completed"])
        self.assertIsNotNone(hold, "export must retain active preservation holds")

    def test_export_cancellation_releases_source_leases(self):
        job_id = self._create()
        cancelled = self.client.post(f"/api/exports/{job_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        result = self._wait_for_status(job_id, "cancelled", "completed", "failed")
        self.assertIn(result["status"], {"cancelled", "completed"})
        self.assertFalse(playback_is_leased(self.media))

    def test_accepted_export_protects_source_during_lease_handoff_and_until_terminal(self):
        with connect() as db:
            # Keep background retention from removing this deliberately old fixture
            # before the export request can reserve it. The test lowers the policy
            # after acceptance to exercise the cleanup/export race itself.
            db.execute(
                "INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,3650)",
                (self.camera,),
            )
            db.execute(
                "UPDATE recording_segments SET starts_at='2020-01-01T10:00:00+00:00',ends_at='2020-01-01T10:00:01+00:00' WHERE id=?",
                (self.segment_id,),
            )
        payload = {
            "camera_ids": [self.camera],
            "start": "2020-01-01T09:59:59.500000Z",
            "end": "2020-01-01T10:00:00.800000Z",
        }
        with patch.object(self.client.app.state.exports, "enqueue"):
            accepted = self.client.post("/api/exports", json=payload)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        job_id = accepted.json()["id"]
        with connect() as db:
            db.execute(
                "UPDATE retention_policy_overrides SET retention_days=1 WHERE scope_type='camera' AND scope_id=?",
                (self.camera,),
            )

        lease_started, allow_lease = threading.Event(), threading.Event()
        cleanup_done = threading.Event()
        cleanup_result: list[dict] = []
        original_acquire = __import__("app.exports", fromlist=["acquire_playback"]).acquire_playback

        def blocked_acquire(path):
            lease_started.set()
            if not allow_lease.wait(5):
                raise TimeoutError("test did not release the lease handoff")
            original_acquire(path)

        manager = ExportManager()
        with patch("app.exports.acquire_playback", side_effect=blocked_acquire):
            executor = threading.Thread(target=lambda: asyncio.run(manager._execute(job_id)))
            executor.start()
            self.assertTrue(lease_started.wait(5), "export did not reach its source lease handoff")

            def run_cleanup():
                cleanup_result.append(cleanup_expired_recordings(datetime.now().astimezone()))
                cleanup_done.set()

            cleaner = threading.Thread(target=run_cleanup)
            cleaner.start()
            self.assertFalse(cleanup_done.wait(0.2), "retention crossed the source selection to lease handoff")
            self.assertTrue(self.media.is_file())
            allow_lease.set()
            cleaner.join(5)
            executor.join(10)
        self.assertFalse(cleaner.is_alive())
        self.assertFalse(executor.is_alive())
        self.assertEqual(cleanup_result[0]["removed_segments"], 0)
        self.assertEqual(cleanup_result[0]["skipped_playback"], 1)
        self.assertEqual(self.client.get(f"/api/exports/{job_id}").json()["status"], "completed")

        cleanup = cleanup_expired_recordings(datetime.now().astimezone())
        self.assertFalse(self.media.exists(), "terminal export should release its persistent source reservation")
        self.assertEqual(cleanup["removed_segments"], 1)

    def test_queue_limit_is_enforced(self):
        for _ in range(4):
            self._create()
        full = self.client.post("/api/exports", json={
            "camera_ids": [self.camera], "start": "2026-10-20T09:59:59Z", "end": "2026-10-20T10:00:02Z",
        })
        self.assertEqual(full.status_code, 429)

    def test_missing_ffmpeg_is_reported_as_a_failed_job(self):
        with patch("app.exports.shutil.which", return_value=None):
            job_id = self._create()
            failed = self._wait_for_status(job_id, "failed")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("FFmpeg is unavailable", failed["error"])

    def test_jobs_are_authenticated_scoped_and_cancelable(self):
        job_id = self._create()
        own = self.client.get(f"/api/exports/{job_id}")
        self.assertEqual(own.status_code, 200)
        app.dependency_overrides[current_user] = lambda: {"id": self.other_user_id, "username": "second", "role": "operator"}
        hidden = self.client.get(f"/api/exports/{job_id}")
        self.assertEqual(hidden.status_code, 404)
        app.dependency_overrides.pop(current_user, None)
        try:
            denied = self.client.get(f"/api/exports/{job_id}")
        finally:
            app.dependency_overrides[current_user] = lambda: {"id": self.user_id, "username": "exporter", "role": "operator"}
        self.assertEqual(denied.status_code, 401)
        cancelled = self.client.post(f"/api/exports/{job_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(self.client.get(f"/api/exports/{job_id}").json()["status"], "cancelled")

    def test_restart_marks_interrupted_job_failed_and_preserves_audit(self):
        # Seed the state a crashed process would leave; the restarted app owns recovery.
        job_id = create_export_record(
            self.user_id, [self.camera],
            datetime.fromisoformat("2026-10-20T09:59:59.500000+00:00"),
            datetime.fromisoformat("2026-10-20T10:00:00.800000+00:00"),
        )
        self.context.__exit__(None, None, None)
        with connect() as db:
            db.execute("UPDATE exports SET status='running' WHERE id=?", (job_id,))
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        result = self.client.get(f"/api/exports/{job_id}").json()
        self.assertEqual(result["status"], "failed")
        self.assertIn("restart", result["error"])
        with connect() as db:
            actions = [row[0] for row in db.execute("SELECT action FROM audit_log WHERE resource_id=? ORDER BY id", (job_id,))]
        self.assertEqual(actions, ["export_requested", "export_failed"])

    def test_creation_rejects_empty_footage_and_bounded_window(self):
        no_footage = self.client.post("/api/exports", json={
            "camera_ids": [self.camera], "start": "2026-10-20T11:00:00Z", "end": "2026-10-20T12:00:00Z",
        })
        self.assertEqual(no_footage.status_code, 422)
        too_long = self.client.post("/api/exports", json={
            "camera_ids": [self.camera], "start": "2026-10-20T00:00:00Z", "end": "2026-10-21T01:00:00Z",
        })
        self.assertEqual(too_long.status_code, 422)


    def test_evidence_package_hashes_bytes_and_verification_is_independent_of_media_probe(self):
        if not self.ffmpeg:
            self.skipTest("FFmpeg is required to create an evidence export")
        job_id = self._create()
        completed = self._wait_for_status(job_id, "completed", "failed")
        self.assertEqual(completed["status"], "completed", completed)
        payload = self.client.get(completed["download_url"]).content
        response = self.client.post(f"/api/exports/{job_id}/verify", content=payload, headers={"content-type": "application/zip"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["checksums_valid"])
        self.assertEqual(result["media_validation"], "not_performed")
        with zipfile.ZipFile(__import__("io").BytesIO(payload)) as archive:
            names = set(archive.namelist())
            metadata = json.loads(archive.read("metadata.json"))
            integrity = json.loads(archive.read("integrity.json"))
        self.assertIn("integrity.json", names)
        self.assertEqual(metadata["audit_reference"], {"resource_type": "export", "resource_id": job_id})
        self.assertIn("UTC", metadata["timestamp_provenance"])
        self.assertEqual(set(item["path"] for item in integrity["files"]), names - {"integrity.json"})
        self.assertNotIn("stream_url", json.dumps(metadata).lower())
        self.assertNotIn("password", json.dumps(metadata).lower())
        with connect() as db:
            entries = db.execute("SELECT actor_id,action,details_json FROM audit_log WHERE resource_id=? ORDER BY id", (job_id,)).fetchall()
        self.assertEqual(entries[-1]["actor_id"], self.user_id)
        self.assertEqual(entries[-1]["action"], "evidence_verified")
        self.assertEqual(json.loads(entries[-1]["details_json"])["outcome"], "valid")

    def test_evidence_verification_detects_modified_delivered_bytes_and_audits_outcome(self):
        if not self.ffmpeg:
            self.skipTest("FFmpeg is required to create an evidence export")
        job_id = self._create()
        completed = self._wait_for_status(job_id, "completed", "failed")
        self.assertEqual(completed["status"], "completed", completed)
        original = self.client.get(completed["download_url"]).content
        changed = __import__("io").BytesIO()
        with zipfile.ZipFile(__import__("io").BytesIO(original)) as source, zipfile.ZipFile(changed, "w", compression=zipfile.ZIP_STORED) as altered:
            for name in source.namelist():
                content = source.read(name)
                if name == "camera-0.mp4":
                    content = content[:-1] + bytes([content[-1] ^ 0x01])
                altered.writestr(name, content)
        response = self.client.post(f"/api/exports/{job_id}/verify", content=changed.getvalue(), headers={"content-type": "application/zip"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["checksums_valid"])
        self.assertTrue(any(not item["matches"] for item in response.json()["files"]))
        with connect() as db:
            row = db.execute("SELECT actor_id,action,details_json FROM audit_log WHERE resource_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        self.assertEqual(row["actor_id"], self.user_id)
        self.assertEqual(row["action"], "evidence_verification_failed")
        self.assertEqual(json.loads(row["details_json"])["outcome"], "invalid")

    def test_evidence_verification_is_authenticated_and_scoped_to_export_owner(self):
        job_id = self._create()
        response = self.client.post(f"/api/exports/{job_id}/verify", content=b"", headers={"content-type": "application/zip"})
        self.assertEqual(response.status_code, 409)
        app.dependency_overrides[current_user] = lambda: {"id": self.other_user_id, "username": "second", "role": "operator"}
        denied = self.client.post(f"/api/exports/{job_id}/verify", content=b"", headers={"content-type": "application/zip"})
        self.assertEqual(denied.status_code, 404)
        app.dependency_overrides.pop(current_user, None)
        try:
            unauthenticated = self.client.post(f"/api/exports/{job_id}/verify", content=b"", headers={"content-type": "application/zip"})
        finally:
            app.dependency_overrides[current_user] = lambda: {"id": self.user_id, "username": "exporter", "role": "operator"}
        self.assertEqual(unauthenticated.status_code, 401)


if __name__ == "__main__":
    unittest.main()
