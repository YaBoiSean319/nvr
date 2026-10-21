from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app, current_user
from app.retention import unlink_if_unleased
from app.store import connect, initialize


class SynchronizedPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH")}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.temporary.name) / "recordings")
        initialize()
        self.cameras = [str(uuid.uuid4()), str(uuid.uuid4())]
        Path(os.environ["NVR_RECORDINGS_PATH"]).mkdir(parents=True)
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'viewer','unused','operator')")
            for number, camera in enumerate(self.cameras, 1):
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (camera, f"Camera {number}"))
            self._segment(db, self.cameras[0], "12:00:00", "12:01:00", "one-a.mp4")
            self._segment(db, self.cameras[0], "12:02:00", "12:03:00", "one-b.mp4")
            second_camera_segment = self._segment(db, self.cameras[1], "12:00:30", "12:02:30", "two.mp4")
            db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,starts_at,ends_at) VALUES(?,?,?,?,?)",
                       (self.cameras[0], "failure", "Camera offline", "2026-10-20T12:01:00+00:00", "2026-10-20T12:02:00+00:00"))
            db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,starts_at,ends_at) VALUES(?,?,?,?,?)",
                       (self.cameras[0], "uncertain", "Motion source degraded", "2026-10-20T12:03:20+00:00", "2026-10-20T12:03:40+00:00"))
            db.execute("INSERT INTO motion_events(id,camera_id,source,source_topic,source_event_at,received_at,requested_start,requested_end,actual_start,actual_end,pre_roll_complete,status,segment_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), self.cameras[1], "onvif", "motion", "2026-10-20T12:00:40+00:00", "2026-10-20T12:00:41+00:00", "2026-10-20T12:00:30+00:00", "2026-10-20T12:01:30+00:00", "2026-10-20T12:00:30+00:00", "2026-10-20T12:01:30+00:00", 1, "complete", second_camera_segment))
        app.dependency_overrides[current_user] = lambda: {"id": 1, "username": "viewer", "role": "operator"}
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def _segment(self, db, camera, start, end, filename):
        relative = f"{camera}/{filename}"
        path = Path(os.environ["NVR_RECORDINGS_PATH"]) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"recording")
        cursor = db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                   (camera, relative, f"2026-10-20T{start}+00:00", f"2026-10-20T{end}+00:00", 9))
        return cursor.lastrowid

    def tearDown(self):
        self.context.__exit__(None, None, None)
        app.dependency_overrides.pop(current_user, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def test_timeline_aligns_camera_intervals_and_reports_motion_and_gaps(self):
        response = self.client.post("/api/playback/timeline", json={
            "camera_ids": self.cameras,
            "start": "2026-10-20T12:00:00Z",
            "end": "2026-10-20T12:04:00Z",
        })
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        first, second = result["cameras"]
        self.assertEqual(first["segments"][0]["starts_at"], "2026-10-20T12:00:00+00:00")
        self.assertEqual(first["gaps"][0]["state"], "failure")
        self.assertEqual(first["gaps"][0]["reason"], "Camera offline")
        self.assertIn("uncertain", [gap["state"] for gap in first["gaps"]])
        self.assertEqual(second["segments"][0]["starts_at"], "2026-10-20T12:00:30+00:00")
        self.assertEqual(second["motion_events"][0]["source_event_at"], "2026-10-20T12:00:40+00:00")
        self.assertIsNotNone(second["motion_events"][0]["media_url"])
        self.assertEqual(first["gaps"][-1]["state"], "missing")

    def test_timeline_rejects_unknown_camera_and_oversized_window(self):
        unknown = self.client.post("/api/playback/timeline", json={
            "camera_ids": [str(uuid.uuid4())], "start": "2026-10-20T12:00:00Z", "end": "2026-10-20T12:01:00Z",
        })
        self.assertEqual(unknown.status_code, 422)
        too_long = self.client.post("/api/playback/timeline", json={
            "camera_ids": [self.cameras[0]], "start": "2026-10-20T00:00:00Z", "end": "2026-10-21T01:00:00Z",
        })
        self.assertEqual(too_long.status_code, 422)

    def test_timeline_rejects_duplicate_cameras_and_reversed_ranges(self):
        duplicate = self.client.post("/api/playback/timeline", json={
            "camera_ids": [self.cameras[0], self.cameras[0]],
            "start": "2026-10-20T12:00:00Z", "end": "2026-10-20T12:01:00Z",
        })
        self.assertEqual(duplicate.status_code, 422)
        reversed_range = self.client.post("/api/playback/timeline", json={
            "camera_ids": [self.cameras[0]],
            "start": "2026-10-20T12:02:00Z", "end": "2026-10-20T12:01:00Z",
        })
        self.assertEqual(reversed_range.status_code, 422)

    def test_timeline_keeps_a_camera_with_no_indexed_footage_visible(self):
        camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (camera_id, "Empty camera"))
        response = self.client.post("/api/playback/timeline", json={
            "camera_ids": [camera_id], "start": "2026-10-20T12:00:00Z", "end": "2026-10-20T12:01:00Z",
        })
        self.assertEqual(response.status_code, 200, response.text)
        camera = response.json()["cameras"][0]
        self.assertEqual(camera["name"], "Empty camera")
        self.assertEqual(camera["segments"], [])
        self.assertTrue(camera["gaps"])
        self.assertEqual(camera["gaps"][0]["state"], "missing")

    def test_snapshot_is_authenticated_bounded_to_interval_and_returns_jpeg(self):
        with connect() as db:
            row = db.execute("SELECT id,relative_path FROM recording_segments WHERE camera_id=? ORDER BY id LIMIT 1", (self.cameras[0],)).fetchone()
            segment = row["id"]
        path = Path(os.environ["NVR_RECORDINGS_PATH"]) / row["relative_path"]
        url = f"/api/cameras/{self.cameras[0]}/segments/{segment}/snapshot?at=2026-10-20T12:00:30Z"
        fake_process = AsyncMock()
        fake_process.returncode = 0
        async def read_snapshot():
            self.assertFalse(unlink_if_unleased(path), "cleanup must not unlink footage while snapshot extraction is reading it")
            return b"\xff\xd8jpeg\xff\xd9", b""
        fake_process.communicate = AsyncMock(side_effect=read_snapshot)
        with patch("app.main.shutil.which", return_value="/usr/bin/ffmpeg"), patch("app.main.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=fake_process) as ffmpeg:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.content, b"\xff\xd8jpeg\xff\xd9")
        self.assertTrue(path.is_file())
        self.assertIn("-ss", ffmpeg.await_args.args)
        outside = self.client.get(f"/api/cameras/{self.cameras[0]}/segments/{segment}/snapshot?at=2026-10-20T12:01:00Z")
        self.assertEqual(outside.status_code, 422)
        app.dependency_overrides.pop(current_user, None)
        try:
            denied = self.client.get(url)
        finally:
            app.dependency_overrides[current_user] = lambda: {"id": 1, "username": "viewer", "role": "operator"}
        self.assertEqual(denied.status_code, 401)

    def test_timeline_requires_authentication(self):
        app.dependency_overrides.pop(current_user, None)
        try:
            denied = self.client.post("/api/playback/timeline", json={
                "camera_ids": [self.cameras[0]], "start": "2026-10-20T12:00:00Z", "end": "2026-10-20T12:01:00Z",
            })
        finally:
            app.dependency_overrides[current_user] = lambda: {"id": 1, "username": "viewer", "role": "operator"}
        self.assertEqual(denied.status_code, 401)


if __name__ == "__main__":
    unittest.main()
