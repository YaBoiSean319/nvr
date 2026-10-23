from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.test_support import patch_camera_probe
from app.credentials import encrypt
from app.main import administrator, app, current_user
from app.recording import audio_recording_args
from app.store import connect, initialize


class CameraAudioApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")}
        root = Path(self.temporary.name)
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        Path(os.environ["NVR_RECORDINGS_PATH"]).mkdir()
        initialize()
        self.camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'operator','unused','operator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(2,'administrator','unused','administrator')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,?,'disabled')",
                       (self.camera_id, "Audio test", encrypt("rtsp://camera.invalid/audio")))
        self.active_user = {"id": 1, "username": "operator", "role": "operator"}
        app.dependency_overrides[current_user] = lambda: self.active_user

        def require_admin():
            if self.active_user["role"] != "administrator":
                raise HTTPException(status_code=403, detail="administrator role required")
            return self.active_user

        app.dependency_overrides[administrator] = require_admin
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        app.dependency_overrides.pop(current_user, None)
        app.dependency_overrides.pop(administrator, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def test_audio_is_off_by_default_admin_only_and_probe_gated(self):
        camera = self.client.get("/api/cameras").json()["cameras"][0]
        self.assertFalse(camera["audio_enabled"])
        self.assertIsNone(camera["audio_supported"])
        self.assertEqual(camera["audio_status"], "unknown")
        self.assertEqual(self.client.patch(f"/api/cameras/{self.camera_id}", json={"audio_enabled": True}).status_code, 403)

        self.active_user = {"id": 2, "username": "administrator", "role": "administrator"}
        unsupported = self.client.patch(f"/api/cameras/{self.camera_id}", json={"audio_enabled": True})
        self.assertEqual(unsupported.status_code, 422)
        with patch_camera_probe(new=AsyncMock(return_value={"reachable": True, "audio": True, "video": {"codec": "h264", "width": 64, "height": 64}})):
            probe = self.client.post(f"/api/cameras/{self.camera_id}/audio/probe")
        self.assertEqual(probe.status_code, 200, probe.text)
        self.assertTrue(probe.json()["audio_supported"])
        saved = self.client.patch(f"/api/cameras/{self.camera_id}", json={"audio_enabled": True})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["audio_enabled"])

        self.active_user = {"id": 1, "username": "operator", "role": "operator"}
        camera = self.client.get("/api/cameras").json()["cameras"][0]
        self.assertEqual(camera["audio_status"], "enabled")
        denied = self.client.patch(f"/api/cameras/{self.camera_id}", json={"audio_enabled": False})
        self.assertEqual(denied.status_code, 403)
        with patch("shutil.which", return_value=None):
            live = self.client.get(f"/api/cameras/{self.camera_id}/audio/live")
        self.assertEqual(live.status_code, 503, live.text)

        self.active_user = {"id": 2, "username": "administrator", "role": "administrator"}
        with patch_camera_probe(new=AsyncMock(return_value={"reachable": True, "audio": False, "video": {"codec": "h264", "width": 64, "height": 64}})):
            lost = self.client.post(f"/api/cameras/{self.camera_id}/audio/probe")
        self.assertEqual(lost.status_code, 200, lost.text)
        self.assertFalse(lost.json()["audio_enabled"])
        camera = self.client.get("/api/cameras").json()["cameras"][0]
        self.assertEqual(camera["audio_status"], "unsupported")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe are required")
class AudioRecordingMediaTests(unittest.TestCase):
    def test_opt_in_audio_is_playable_aac_and_default_muted_output_has_no_audio_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "camera-source.mp4"
            subprocess.run([
                shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "testsrc=size=64x64:rate=5:duration=1", "-f", "lavfi", "-i",
                "sine=frequency=1000:sample_rate=44100:duration=1", "-shortest", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(source),
            ], check=True, timeout=30)
            results = {}
            for enabled, name in ((True, "with-audio.mp4"), (False, "without-audio.mp4")):
                output = root / name
                subprocess.run([
                    shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(source),
                    "-map", "0:v:0", *audio_recording_args(enabled), "-c:v", "copy", "-movflags", "+faststart",
                    "-y", str(output),
                ], check=True, timeout=30)
                probe = subprocess.run([
                    shutil.which("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                    "-of", "json", str(output),
                ], check=True, capture_output=True, text=True, timeout=15)
                results[enabled] = json.loads(probe.stdout)["streams"]
            self.assertIn({"codec_type": "audio", "codec_name": "aac"}, results[True])
            self.assertFalse(any(stream["codec_type"] == "audio" for stream in results[False]))


if __name__ == "__main__":
    unittest.main()
