import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NVR_DB_PATH"] = str(Path(self.tmp.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.tmp.name) / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.tmp.name) / "credentials.key")
        os.environ["NVR_COOKIE_SECURE"] = "false"
        os.environ["NVR_SEGMENT_SECONDS"] = "10"
        from app.main import app
        self.client_ctx = TestClient(app)
        self.client = self.client_ctx.__enter__()

    def tearDown(self):
        self.client_ctx.__exit__(None, None, None)
        self.tmp.cleanup()
        os.environ.pop("NVR_DB_PATH", None)
        os.environ.pop("NVR_RECORDINGS_PATH", None)
        os.environ.pop("NVR_CREDENTIAL_KEY_PATH", None)
        os.environ.pop("NVR_COOKIE_SECURE", None)
        os.environ.pop("NVR_SEGMENT_SECONDS", None)

    def test_bootstrap_closes_setup_and_authenticates_admin(self):
        self.assertEqual(self.client.get("/api/setup/status").json(), {"setup_required": True})
        response = self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["role"], "administrator")
        self.assertEqual(self.client.get("/api/setup/status").json(), {"setup_required": False})
        self.assertEqual(self.client.get("/api/system/status").status_code, 200)
        self.assertEqual(self.client.post("/api/setup/admin", json={"username":"second-admin", "password":"AnotherStrongPass2026"}).status_code, 409)

    def test_api_denies_status_and_invalid_credentials(self):
        self.assertEqual(self.client.get("/api/system/status").status_code, 401)
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.client.post("/api/auth/logout")
        bad = self.client.post("/api/auth/login", json={"username":"local-admin", "password":"wrong"})
        self.assertEqual(bad.status_code, 401)

    def test_restart_preserves_account_and_health(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.client_ctx.__exit__(None, None, None)
        from app.main import app
        with TestClient(app) as restarted:
            self.assertEqual(restarted.get("/healthz").status_code, 200)
            self.assertEqual(restarted.get("/api/setup/status").json(), {"setup_required": False})
            self.assertEqual(restarted.get("/api/system/status").status_code, 401)
            self.assertEqual(restarted.post("/api/auth/login", json={"username":"local-admin", "password":"StrongPassphrase2026"}).status_code, 200)

    def test_password_policy_and_username_validation(self):
        self.assertEqual(self.client.post("/api/setup/admin", json={"username":"a", "password":"short"}).status_code, 422)
        self.assertEqual(self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"longpasswordonly"}).status_code, 422)

    def test_camera_credentials_are_encrypted_and_not_returned(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        stream_url = "rtsp://viewer:CameraSecret2026@127.0.0.1:554/stream?token=NeverReturnThis"
        probe = {"reachable": True, "video": {"codec":"h264", "width":640, "height":360}}
        with patch("app.main.probe_stream", new=AsyncMock(return_value=probe)):
            added = self.client.post("/api/cameras", json={"name":"Test Camera", "stream_url":stream_url})
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.json()["recording_mode"], "motion")
        self.assertEqual(added.json()["recording_status"], "unsupported")
        response_text = added.text + self.client.get("/api/cameras").text
        self.assertNotIn("CameraSecret2026", response_text)
        self.assertNotIn("NeverReturnThis", response_text)
        self.assertIn("rtsp://127.0.0.1:554", response_text)
        self.assertNotIn("/stream", response_text)
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            ciphertext = db.execute("SELECT stream_url_ciphertext FROM cameras").fetchone()[0]
        self.assertNotIn(b"CameraSecret2026", ciphertext)
        self.assertEqual(Path(os.environ["NVR_CREDENTIAL_KEY_PATH"]).stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(__import__("shutil").which("ffprobe"), "FFprobe is required for the stream test")
    def test_real_probe_failure_is_reported_without_echoing_credentials(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        response = self.client.post("/api/cameras/test", json={
            "stream_url":"rtsp://probe-user:ProbeSecret2026@127.0.0.1:1/unavailable"
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["reachable"])
        self.assertNotIn("ProbeSecret2026", response.text)
        self.assertEqual(response.json()["reason"], "Camera stream could not be opened")

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "FFmpeg is required for reconnect testing")
    def test_continuous_recorder_reconnects_after_stream_failure(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        capability = {"reachable": True, "video": {"codec":"h264", "width":640, "height":360}}
        with patch("app.main.probe_stream", new=AsyncMock(return_value=capability)):
            created = self.client.post("/api/cameras", json={
                "name":"Unavailable test feed",
                "stream_url":"rtsp://feed-user:FeedSecret2026@127.0.0.1:1/stream",
                "recording_mode":"continuous",
            })
        self.assertEqual(created.status_code, 201)
        camera_id = created.json()["id"]
        deadline = time.monotonic() + 8
        current = created.json()
        while time.monotonic() < deadline:
            current = self.client.get("/api/cameras").json()["cameras"][0]
            if current["recording_status"] == "reconnecting":
                break
            time.sleep(0.2)
        self.assertEqual(current["recording_status"], "reconnecting")
        self.assertNotIn("FeedSecret2026", self.client.get("/api/cameras").text)
        stopped = self.client.patch(f"/api/cameras/{camera_id}", json={"recording_mode":"disabled"})
        self.assertEqual(stopped.status_code, 200)

    @unittest.skipUnless(os.environ.get("NVR_RTSP_TEST_URL"), "A simulated RTSP publisher is required")
    def test_rtsp_stream_is_probed_recorded_indexed_and_playable(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        created = self.client.post("/api/cameras", json={
            "name":"Simulated RTSP camera",
            "stream_url":os.environ["NVR_RTSP_TEST_URL"],
            "recording_mode":"continuous",
        })
        self.assertEqual(created.status_code, 201, created.text)
        camera_id = created.json()["id"]
        self.assertTrue(created.json()["video"]["codec"])
        deadline = time.monotonic() + 35
        segments = []
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/cameras/{camera_id}/segments")
            self.assertEqual(response.status_code, 200)
            segments = response.json()["segments"]
            if segments:
                break
            time.sleep(1)
        self.assertTrue(segments, "FFmpeg did not index a completed segment from the RTSP stream")
        playback = self.client.get(segments[0]["media_url"])
        self.assertEqual(playback.status_code, 200)
        self.assertEqual(playback.headers["content-type"], "video/mp4")
        self.assertIn("inline", playback.headers["content-disposition"])
        self.assertGreater(len(playback.content), 128)
        stopped = self.client.patch(f"/api/cameras/{camera_id}", json={"recording_mode":"disabled"})
        self.assertEqual(stopped.status_code, 200)

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "FFmpeg is required for the media response test")
    def test_authenticated_browser_media_route_serves_indexed_video(self):
        media_root = Path(os.environ["NVR_RECORDINGS_PATH"])
        camera_id = str(uuid.uuid4())
        directory = media_root / camera_id / uuid.uuid4().hex
        directory.mkdir(parents=True)
        index_path = directory / "segments.csv"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "testsrc=duration=2:size=64x64:rate=10", "-an", "-c:v", "libx264", "-f", "segment",
            "-segment_time", "60", "-reset_timestamps", "1", "-segment_format", "mp4",
            "-segment_format_options", "movflags=+faststart", "-segment_list", str(index_path),
            "-segment_list_type", "csv", "-strftime", "1", str(directory / "segment-%Y%m%dT%H%M%SZ.mp4"),
        ], check=True, timeout=20)
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        from app.credentials import encrypt
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute(
                "INSERT INTO cameras(id, name, stream_url_ciphertext, recording_mode) VALUES (?, ?, ?, 'disabled')",
                (camera_id, "Playback camera", encrypt("rtsp://viewer:secret@camera/stream")),
            )
        from app.recording import RecordingManager
        self.assertEqual(RecordingManager()._index_csv(camera_id, index_path, directory), 1)
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            segment_id = db.execute("SELECT id FROM recording_segments").fetchone()[0]
            relative_path = db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (segment_id,)).fetchone()[0]
        media = media_root / relative_path
        anonymous = self.client.post("/api/auth/logout")
        self.assertEqual(anonymous.status_code, 204)
        denied = self.client.get(f"/api/cameras/{camera_id}/segments/{segment_id}/media")
        self.assertEqual(denied.status_code, 401)
        self.client.post("/api/auth/login", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        response = self.client.get(f"/api/cameras/{camera_id}/segments/{segment_id}/media")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertGreater(len(response.content), 100)
        partial = self.client.get(
            f"/api/cameras/{camera_id}/segments/{segment_id}/media", headers={"Range":"bytes=0-99"}
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(len(partial.content), 100)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(media)],
            capture_output=True, text=True, check=True, timeout=10,
        )
        self.assertGreater(float(probe.stdout.strip()), 0)


if __name__ == "__main__":
    unittest.main()
