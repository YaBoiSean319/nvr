from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from tests.test_support import TEST_PASSWORD
from app.store import connect, initialize


class CameraArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in (
            "NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_COOKIE_SECURE",
        )}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.temporary.name) / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.temporary.name) / "credentials.key")
        os.environ["NVR_COOKIE_SECURE"] = "false"
        initialize()
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.client.post("/api/setup/admin", json={"username": "archive-admin", "password": TEST_PASSWORD})
        self.camera_id = self.add_camera()
        self.recordings = Path(os.environ["NVR_RECORDINGS_PATH"])

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temporary.cleanup()
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def add_camera(self) -> str:
        capability = {"reachable": True, "video": {"codec": "h264", "width": 640, "height": 360}}
        with patch("app.main.probe_stream", new=AsyncMock(return_value=capability)):
            response = self.client.post("/api/cameras", json={
                "name": "Archive camera", "stream_url": "rtsp://camera.example/live", "recording_mode": "disabled",
            })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def add_segment(self, name: str = "segment.mp4") -> tuple[int, Path]:
        relative = f"{self.camera_id}/{name}"
        path = self.recordings / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"recorded evidence")
        with connect() as db:
            segment_id = db.execute(
                "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                (self.camera_id, relative, "2026-10-21T10:00:00+00:00", "2026-10-21T10:01:00+00:00", path.stat().st_size),
            ).lastrowid
        return int(segment_id), path

    def test_remove_archives_camera_and_keeps_indexed_playable_footage(self):
        segment_id, media = self.add_segment()
        response = self.client.delete(f"/api/cameras/{self.camera_id}")
        self.assertEqual(response.status_code, 204, response.text)

        inventory = self.client.get("/api/cameras").json()["cameras"]
        archived = next(camera for camera in inventory if camera["id"] == self.camera_id)
        self.assertTrue(archived["archived_at"])
        self.assertFalse(archived["enabled"])
        self.assertEqual(archived["recording_status"], "archived")
        self.assertTrue(media.is_file())

        timeline = self.client.post("/api/playback/timeline", json={
            "camera_ids": [self.camera_id], "start": "2026-10-21T10:00:00Z", "end": "2026-10-21T10:01:00Z",
        })
        self.assertEqual(timeline.status_code, 200, timeline.text)
        self.assertEqual(timeline.json()["cameras"][0]["segments"][0]["id"], segment_id)
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera_id}/segments").json()["segments"][0]["id"], segment_id)
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera_id}/segments/{segment_id}/media").status_code, 200)
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera_id}/live/status").status_code, 410)
        restored = self.client.post(f"/api/cameras/{self.camera_id}/restore")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertIsNone(restored.json()["archived_at"])
        self.assertTrue(restored.json()["enabled"])
        self.assertFalse(restored.json()["recording_enabled"])

    def test_explicit_footage_deletion_refuses_preserved_segments_and_keeps_media(self):
        segment_id, media = self.add_segment()
        preserved = self.client.post(f"/api/cameras/{self.camera_id}/segments/{segment_id}/hold", json={})
        self.assertEqual(preserved.status_code, 201, preserved.text)
        archived = self.client.delete(f"/api/cameras/{self.camera_id}")
        self.assertEqual(archived.status_code, 204, archived.text)

        deleted = self.client.request(
            "DELETE", f"/api/cameras/{self.camera_id}/footage", json={"confirm_camera_name": "Archive camera"},
        )
        self.assertEqual(deleted.status_code, 409, deleted.text)
        self.assertTrue(media.is_file())
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera_id}/segments").json()["segments"][0]["id"], segment_id)

    def test_explicit_footage_deletion_refuses_active_exports_without_partial_removal(self):
        from datetime import datetime, timezone
        from app.exports import create_export_record

        _, media = self.add_segment()
        export_id = create_export_record(
            1, [self.camera_id], datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 21, 10, 1, tzinfo=timezone.utc),
        )
        self.client.delete(f"/api/cameras/{self.camera_id}")
        deleted = self.client.request(
            "DELETE", f"/api/cameras/{self.camera_id}/footage", json={"confirm_camera_name": "Archive camera"},
        )
        self.assertEqual(deleted.status_code, 409, deleted.text)
        self.assertTrue(media.is_file())
        with connect() as db:
            self.assertEqual(db.execute("SELECT status FROM exports WHERE id=?", (export_id,)).fetchone()["status"], "queued")

    def test_explicit_footage_deletion_removes_only_media_and_keeps_archived_identity(self):
        segment_id, media = self.add_segment()
        self.assertEqual(self.client.delete(f"/api/cameras/{self.camera_id}").status_code, 204)
        deleted = self.client.request(
            "DELETE", f"/api/cameras/{self.camera_id}/footage", json={"confirm_camera_name": "Archive camera"},
        )
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertFalse(media.exists())
        camera = next(row for row in self.client.get("/api/cameras").json()["cameras"] if row["id"] == self.camera_id)
        self.assertTrue(camera["archived_at"])
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera_id}/segments").json()["segments"], [])
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM recording_segments WHERE id=?", (segment_id,)).fetchone())
            audit = db.execute("SELECT action,details_json FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(audit["action"], "delete_camera_footage")
        self.assertEqual(json.loads(audit["details_json"])["segments_deleted"], 1)

    def test_operators_cannot_archive_or_explicitly_delete_camera_footage(self):
        from app.security import hash_password

        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", (
                "archive-operator", hash_password(TEST_PASSWORD), "operator",
            ))
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/auth/login", json={
            "username": "archive-operator", "password": TEST_PASSWORD,
        }).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/cameras/{self.camera_id}").status_code, 403)
        denied = self.client.request(
            "DELETE", f"/api/cameras/{self.camera_id}/footage", json={"confirm_camera_name": "Archive camera"},
        )
        self.assertEqual(denied.status_code, 403)

    def test_existing_camera_databases_migrate_archive_state(self):
        with connect() as db:
            db.execute("ALTER TABLE cameras DROP COLUMN archived_at")
        initialize()
        with connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(cameras)")}
        self.assertIn("archived_at", columns)
        camera = next(row for row in self.client.get("/api/cameras").json()["cameras"] if row["id"] == self.camera_id)
        self.assertIsNone(camera["archived_at"])


if __name__ == "__main__":
    unittest.main()
