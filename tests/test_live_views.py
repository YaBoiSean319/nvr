from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.credentials import encrypt
from app.api.live_media import _acquire_live_view, _release_live_audio, _release_live_view, live_camera, live_camera_audio
from app.main import app, current_user
from app.store import connect, initialize


class LiveViewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH")}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.temporary.name) / "recordings")
        initialize()
        self.user_id = 1
        self.other_user_id = 2
        self.camera = str(uuid.uuid4())
        self.camera_two = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'viewer','unused','operator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(2,'other','unused','operator')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (self.camera, "Front Door"))
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (self.camera_two, "Loading Bay"))
        self.active_user = {"id": self.user_id, "username": "viewer", "role": "operator"}
        app.dependency_overrides[current_user] = lambda: self.active_user
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

    def test_operator_saves_private_grid_and_cannot_reference_unknown_cameras(self):
        payload = {"name": "Opening shift", "camera_ids": [self.camera, self.camera_two]}
        saved = self.client.put("/api/live/layouts/Opening%20shift", json={"camera_ids": payload["camera_ids"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json(), {"name": "Opening shift", "camera_ids": payload["camera_ids"]})
        self.active_user = {"id": self.other_user_id, "username": "other", "role": "operator"}
        self.assertEqual(self.client.get("/api/live/layouts").json(), {"layouts": []})
        invalid = self.client.put("/api/live/layouts/Bad", json={"camera_ids": [str(uuid.uuid4())]})
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.active_user = {"id": self.user_id, "username": "viewer", "role": "operator"}
        self.assertEqual(self.client.get("/api/live/layouts").json()["layouts"], [{"name": "Opening shift", "camera_ids": payload["camera_ids"]}])

    def test_saved_live_grid_accepts_sixteen_cameras_and_rejects_seventeen(self):
        camera_ids = [self.camera, self.camera_two]
        with connect() as db:
            for index in range(14):
                camera_id = str(uuid.uuid4())
                camera_ids.append(camera_id)
                db.execute(
                    "INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')",
                    (camera_id, f"Camera {index + 3}"),
                )

        accepted = self.client.put("/api/live/layouts/Sixteen", json={"camera_ids": camera_ids})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(len(accepted.json()["camera_ids"]), 16)

        extra_id = str(uuid.uuid4())
        with connect() as db:
            db.execute(
                "INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')",
                (extra_id, "Camera 17"),
            )
        rejected = self.client.put("/api/live/layouts/Seventeen", json={"camera_ids": camera_ids + [extra_id]})
        self.assertEqual(rejected.status_code, 422, rejected.text)

    def test_live_view_requires_authenticated_camera_and_valid_quality(self):
        unknown = self.client.get(f"/api/cameras/{uuid.uuid4()}/live?quality=low")
        self.assertEqual(unknown.status_code, 404)
        invalid = self.client.get(f"/api/cameras/{self.camera}/live?quality=ultra")
        self.assertEqual(invalid.status_code, 422)

    def test_low_quality_uses_encrypted_substream_and_releases_view_slot(self):
        with connect() as db:
            db.execute(
                "UPDATE cameras SET stream_url_ciphertext=?,live_substream_url_ciphertext=? WHERE id=?",
                (encrypt("rtsp://camera.local/primary"), encrypt("rtsp://camera.local/low"), self.camera),
            )
        observed = []

        async def fake_frames(camera_id, source, quality):
            observed.append((camera_id, source, quality))
            yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 2\r\n\r\nxx\r\n"

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), patch("app.api.live_media._mjpeg_frames", side_effect=fake_frames):
            response = self.client.get(f"/api/cameras/{self.camera}/live?quality=low")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("multipart/x-mixed-replace", response.headers["content-type"])
            self.assertNotIn("rtsp://", response.text)
            self.assertEqual(observed, [(self.camera, "rtsp://camera.local/low", "low")])
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera}/live/status").json()["active_viewers"], 0)

    def test_full_video_capacity_allows_sixteen_bounded_audio_listeners(self):
        audio_cameras = [self.camera, self.camera_two]
        with connect() as db:
            db.execute(
                "UPDATE cameras SET stream_url_ciphertext=?,audio_supported=1,audio_enabled=1 WHERE id=?",
                (encrypt("rtsp://camera.local/audio"), self.camera),
            )
            db.execute(
                "UPDATE cameras SET stream_url_ciphertext=?,audio_supported=1,audio_enabled=1 WHERE id=?",
                (encrypt("rtsp://camera.local/audio-two"), self.camera_two),
            )
            for index in range(15):
                camera_id = str(uuid.uuid4())
                audio_cameras.append(camera_id)
                db.execute(
                    "INSERT INTO cameras(id,name,stream_url_ciphertext,audio_supported,audio_enabled,recording_mode) VALUES(?,?,?,1,1,'disabled')",
                    (camera_id, f"Audio camera {index + 3}", encrypt(f"rtsp://camera.local/audio-{index + 3}")),
                )

        reservations = [f"occupied-{index}" for index in range(15)]
        for camera_id in reservations:
            self.assertTrue(_acquire_live_view(camera_id))
        video_acquired = False
        audio_acquired = 0
        try:
            with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
                video = live_camera(self.camera, {}, "low")
                video_acquired = True
                self.assertEqual(video.media_type, "multipart/x-mixed-replace; boundary=frame")
                with self.assertRaises(HTTPException) as video_limit:
                    live_camera(self.camera_two, {}, "low")
                self.assertEqual(video_limit.exception.status_code, 503)
                for camera_id in audio_cameras[:16]:
                    audio = live_camera_audio(camera_id, {})
                    audio_acquired += 1
                    self.assertEqual(audio.media_type, "audio/mpeg")
                with self.assertRaises(HTTPException) as bounded:
                    live_camera_audio(audio_cameras[16], {})
                self.assertEqual(bounded.exception.status_code, 503)
        finally:
            if video_acquired:
                _release_live_view(self.camera)
            for _ in range(audio_acquired):
                _release_live_audio()
            for camera_id in reservations:
                _release_live_view(camera_id)

if __name__ == "__main__":
    unittest.main()
