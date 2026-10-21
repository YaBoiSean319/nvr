from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from unittest.mock import patch

from app.credentials import encrypt
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

        with patch("app.main.shutil.which", return_value="/usr/bin/ffmpeg"), patch("app.main._mjpeg_frames", side_effect=fake_frames):
            response = self.client.get(f"/api/cameras/{self.camera}/live?quality=low")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("multipart/x-mixed-replace", response.headers["content-type"])
            self.assertNotIn("rtsp://", response.text)
            self.assertEqual(observed, [(self.camera, "rtsp://camera.local/low", "low")])
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera}/live/status").json()["active_viewers"], 0)


if __name__ == "__main__":
    unittest.main()
