from __future__ import annotations

import os
import tempfile
import unittest
import uuid
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import administrator, app, current_user
from app.recording import RecordingManager
from app.store import connect, initialize


class IncidentApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_STORAGE_VOLUMES", "NVR_STORAGE_PRESSURE_FREE_BYTES")}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        self.recordings_root = Path(self.temporary.name) / "recordings"
        self.recordings_root.mkdir()
        os.environ["NVR_RECORDINGS_PATH"] = str(self.recordings_root)
        os.environ["NVR_STORAGE_VOLUMES"] = json.dumps({"default": ""})
        os.environ.pop("NVR_STORAGE_PRESSURE_FREE_BYTES", None)
        initialize()
        self.camera_id = str(uuid.uuid4())
        now = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('operator','unused','operator')")
            self.operator_id = int(db.execute("SELECT id FROM users WHERE username='operator'").fetchone()[0])
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('administrator','unused','administrator')")
            self.admin_id = int(db.execute("SELECT id FROM users WHERE username='administrator'").fetchone()[0])
            db.execute(
                "INSERT INTO cameras(id,name,stream_url_ciphertext,recording_enabled,recording_mode,recording_status,last_video_at,last_footage_write_at) "
                "VALUES(?,?,X'01',1,'continuous','reconnecting',?,?)",
                (self.camera_id, "Incident camera", now, now),
            )
        self.active_user = {"id": self.operator_id, "username": "operator", "role": "operator"}
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

    def test_active_recording_fault_can_be_acknowledged_recovered_and_recur_after_restart(self):
        manager = RecordingManager()
        manager._sync_coverage(self.camera_id)
        response = self.client.get("/api/incidents")
        self.assertEqual(response.status_code, 200, response.text)
        incidents = response.json()["incidents"]
        self.assertEqual(len(incidents), 1)
        incident_id = incidents[0]["id"]
        self.assertEqual(incidents[0]["state"], "active")
        self.assertTrue(incidents[0]["fault_active"])

        acknowledged = self.client.post(f"/api/incidents/{incident_id}/acknowledge")
        self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
        self.assertEqual(acknowledged.json()["state"], "acknowledged")
        self.assertTrue(acknowledged.json()["fault_active"])

        manager._sync_coverage(self.camera_id)
        listing = self.client.get("/api/incidents").json()["incidents"]
        self.assertEqual(len(listing), 1, "repeated observations should update the existing incident")
        self.assertEqual(listing[0]["state"], "acknowledged", "acknowledgement must not clear the active fault")

        now = datetime.now(timezone.utc).isoformat()
        with connect() as db:
            db.execute(
                "UPDATE cameras SET recording_status='recording',last_video_at=?,last_footage_write_at=? WHERE id=?",
                (now, now, self.camera_id),
            )
        manager._sync_coverage(self.camera_id)
        recovered = self.client.get("/api/incidents").json()["incidents"][0]
        self.assertEqual(recovered["state"], "recovered")
        self.assertFalse(recovered["fault_active"])

        self.context.__exit__(None, None, None)
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        persisted = self.client.get("/api/incidents").json()["incidents"][0]
        self.assertEqual(persisted["id"], incident_id)
        self.assertEqual(persisted["state"], "recovered")
        with connect() as db:
            old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            db.execute(
                "UPDATE cameras SET recording_status='reconnecting',last_footage_write_at=? WHERE id=?",
                (old, self.camera_id),
            )
        manager._sync_coverage(self.camera_id)
        recurrent = self.client.get("/api/incidents").json()["incidents"][0]
        self.assertEqual(recurrent["id"], incident_id)
        self.assertEqual(recurrent["state"], "active")
        self.assertEqual(recurrent["recurrence_count"], 2)
        self.assertEqual([event["event_type"] for event in recurrent["events"]], ["raised", "acknowledged", "recovered", "recurred"])

    def test_operator_cannot_acknowledge_unknown_incident(self):
        response = self.client.post("/api/incidents/not-an-incident/acknowledge")
        self.assertEqual(response.status_code, 404)

    def test_storage_pressure_groups_affected_cameras_and_recovers(self):
        os.environ["NVR_STORAGE_PRESSURE_FREE_BYTES"] = "9999999999999999"
        self.active_user = {"id": self.admin_id, "username": "administrator", "role": "administrator"}
        volume = self.client.get("/api/storage")
        self.assertEqual(volume.status_code, 200, volume.text)
        self.active_user = {"id": self.operator_id, "username": "operator", "role": "operator"}
        incident = self.client.get("/api/incidents").json()["incidents"][0]
        self.assertEqual(incident["fault_type"], "storage")
        self.assertTrue(incident["fault_active"])
        self.assertEqual(incident["details"]["affected_cameras"][0]["id"], self.camera_id)

        os.environ["NVR_STORAGE_PRESSURE_FREE_BYTES"] = "0"
        self.active_user = {"id": self.admin_id, "username": "administrator", "role": "administrator"}
        self.client.get("/api/storage")
        self.active_user = {"id": self.operator_id, "username": "operator", "role": "operator"}
        incident = self.client.get("/api/incidents").json()["incidents"][0]
        self.assertEqual(incident["state"], "recovered")


if __name__ == "__main__":
    unittest.main()
