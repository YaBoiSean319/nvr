from __future__ import annotations

import os
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.credentials import encrypt
from app.detection import DetectionManager, MODEL_ID, MODEL_VERSION, point_in_zones, record_observations
from app.main import administrator, app, current_user
from app.store import connect, initialize


class DetectionApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")}
        self.root = Path(self.temporary.name)
        os.environ["NVR_DB_PATH"] = str(self.root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(self.root / "credentials.key")
        Path(os.environ["NVR_RECORDINGS_PATH"]).mkdir()
        initialize()
        self.camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'operator','unused','operator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(2,'administrator','unused','administrator')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,live_substream_url_ciphertext,recording_mode,recording_enabled) VALUES(?,?,?,?,?,?)",
                       (self.camera_id, "AI camera", encrypt("rtsp://camera.invalid/main"), encrypt("rtsp://camera.invalid/low"), "continuous", 1))
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

    def test_admin_can_configure_detection_and_operator_can_search_but_not_change_it(self):
        denied = self.client.put(f"/api/cameras/{self.camera_id}/ai", json={"enabled": True})
        self.assertEqual(denied.status_code, 403)
        self.active_user = {"id": 2, "username": "administrator", "role": "administrator"}
        manager = app.state.detectors
        manager.detector = object()
        saved = self.client.put(f"/api/cameras/{self.camera_id}/ai", json={
            "enabled": True, "analysis_fps": 0.5, "confidence_threshold": 0.6,
            "use_substream": True, "zones": [{"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8}],
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["enabled"])
        self.assertEqual(saved.json()["model"]["model_id"], MODEL_ID)
        self.assertEqual(saved.json()["status"], "connecting")
        coverage = self.client.get(f"/api/cameras/{self.camera_id}/ai/coverage")
        self.assertEqual(coverage.status_code, 200, coverage.text)
        self.assertEqual(coverage.json()["intervals"][0]["state"], "unanalyzed")
        self.active_user = {"id": 1, "username": "operator", "role": "operator"}
        self.assertEqual(self.client.put(f"/api/cameras/{self.camera_id}/ai", json={"enabled": False}).status_code, 403)

        event_at = datetime.now(timezone.utc)
        record_observations(self.camera_id, [{"label": "person", "confidence": 0.91, "box": {"x": 0.2, "y": 0.2, "width": 0.1, "height": 0.3}}], event_at, model_id="fixture-compatible", model_version="sha256:fixture-1", provider="GPU")
        start = (event_at - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        end = (event_at + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        results = self.client.get(f"/api/ai/events?camera_id={self.camera_id}&label=person&start={start}&end={end}")
        self.assertEqual(results.status_code, 200, results.text)
        item = results.json()["events"][0]
        self.assertEqual(item["label"], "person")
        self.assertEqual(item["model_id"], "fixture-compatible")
        self.assertEqual(item["model_version"], "sha256:fixture-1")
        self.assertEqual(item["provider"], "GPU")
        self.assertIn("do not prove", results.json()["coverage_note"])

    def test_invalid_zones_and_substream_are_rejected_and_only_one_camera_can_be_enabled(self):
        self.active_user = {"id": 2, "username": "administrator", "role": "administrator"}
        invalid = self.client.put(f"/api/cameras/{self.camera_id}/ai", json={
            "enabled": True, "zones": [{"x": 0.9, "y": 0, "width": 0.2, "height": 1}],
        })
        self.assertEqual(invalid.status_code, 422)
        with connect() as db:
            db.execute("UPDATE cameras SET live_substream_url_ciphertext=NULL WHERE id=?", (self.camera_id,))
        no_substream = self.client.put(f"/api/cameras/{self.camera_id}/ai", json={"enabled": True, "use_substream": True})
        self.assertEqual(no_substream.status_code, 422)
        enabled = self.client.put(f"/api/cameras/{self.camera_id}/ai", json={"enabled": True, "use_substream": False})
        self.assertEqual(enabled.status_code, 200, enabled.text)
        second = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (second, "Other camera", encrypt("rtsp://camera.invalid/other")))
        conflict = self.client.put(f"/api/cameras/{second}/ai", json={"enabled": True, "use_substream": False})
        self.assertEqual(conflict.status_code, 200, conflict.text)

    def test_zone_filter_uses_detection_center_and_empty_zone_covers_frame(self):
        box = {"x": 0.45, "y": 0.2, "width": 0.1, "height": 0.2}
        self.assertTrue(point_in_zones(box, [{"x": 0.4, "y": 0.1, "width": 0.2, "height": 0.4}]))
        self.assertFalse(point_in_zones(box, [{"x": 0, "y": 0, "width": 0.3, "height": 1}]))
        self.assertTrue(point_in_zones(box, []))

    def test_group_inheritance_camera_override_and_bounded_camera_capacity(self):
        self.active_user = {"id": 2, "username": "administrator", "role": "administrator"}
        manager: DetectionManager = app.state.detectors

        class QuietDetector:
            def infer(self, frame, threshold):
                return []

        manager.detector = QuietDetector()
        group = self.client.post("/api/camera-groups", json={"name": "AI group"})
        self.assertEqual(group.status_code, 201, group.text)
        group_id = group.json()["id"]
        second = str(uuid.uuid4())
        with connect() as db:
            db.execute("UPDATE cameras SET group_id=? WHERE id=?", (group_id, self.camera_id))
            db.execute("INSERT INTO cameras(id,name,group_id,stream_url_ciphertext,live_substream_url_ciphertext) VALUES(?,?,?,?,?)",
                       (second, "AI group second", group_id, encrypt("rtsp://camera.invalid/second"), encrypt("rtsp://camera.invalid/second-low")))
        capacity = self.client.put("/api/ai/settings", json={"max_active_cameras": 1})
        self.assertEqual(capacity.status_code, 200, capacity.text)
        group_config = self.client.put(f"/api/camera-groups/{group_id}/ai", json={"enabled": True, "analysis_fps": 0.5, "use_substream": True})
        self.assertEqual(group_config.status_code, 200, group_config.text)
        self.assertEqual(group_config.json()["inherited_camera_count"], 2)
        active = [task for task in manager.tasks.values() if not task.done()]
        self.assertEqual(len(active), 1)
        with connect() as db:
            statuses = [row[0] for row in db.execute("SELECT status FROM ai_camera_settings WHERE camera_id IN (?,?)", (self.camera_id, second))]
        self.assertIn("skipped", statuses)

        effective = self.client.get(f"/api/cameras/{self.camera_id}/ai").json()
        self.assertEqual(effective["config_source"], "group")
        self.assertTrue(effective["enabled"])
        override = self.client.put(f"/api/cameras/{self.camera_id}/ai", json={"enabled": False, "use_substream": False})
        self.assertEqual(override.status_code, 200, override.text)
        self.assertEqual(override.json()["config_source"], "camera")
        self.assertFalse(override.json()["enabled"])
        inherited = self.client.put(f"/api/cameras/{self.camera_id}/ai", json={"inherit_group": True})
        self.assertEqual(inherited.status_code, 200, inherited.text)
        self.assertEqual(inherited.json()["config_source"], "group")
        self.assertTrue(inherited.json()["enabled"])

    def test_recording_policy_and_worker_are_separate_from_ai_stop(self):
        manager: DetectionManager = app.state.detectors
        manager.detector = object()
        self.active_user = {"id": 2, "username": "administrator", "role": "administrator"}
        response = self.client.put(f"/api/cameras/{self.camera_id}/ai", json={"enabled": True, "use_substream": False})
        self.assertEqual(response.status_code, 200, response.text)
        recorder_task = app.state.recorders.tasks[self.camera_id]
        stopped = self.client.put(f"/api/cameras/{self.camera_id}/ai", json={"enabled": False, "use_substream": False})
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertIs(app.state.recorders.tasks[self.camera_id], recorder_task)
        with connect() as db:
            camera = db.execute("SELECT recording_enabled,recording_mode FROM cameras WHERE id=?", (self.camera_id,)).fetchone()
        self.assertEqual((camera["recording_enabled"], camera["recording_mode"]), (1, "continuous"))

    def test_runtime_provider_and_model_are_validated_before_activation(self):
        self.active_user = {"id": 2, "username": "administrator", "role": "administrator"}
        candidate = {"provider": "GPU", "model_id": MODEL_ID}
        with patch("app.main.validate_detector", return_value={"valid": False, "reason": "GPU unavailable"}):
            preview = self.client.post("/api/ai/settings/validate", json=candidate)
            rejected = self.client.put("/api/ai/settings", json={"max_active_cameras": 2, **candidate})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertFalse(preview.json()["valid"])
        self.assertEqual(rejected.status_code, 422, rejected.text)
        with connect() as db:
            runtime = db.execute("SELECT provider,model_id FROM ai_runtime_settings WHERE singleton_id=1").fetchone()
        self.assertEqual((runtime["provider"], runtime["model_id"]), ("CPU", MODEL_ID))

        with patch("app.main.validate_detector", return_value={"valid": True, "model_version": "sha256:fixture-1"}):
            activated = self.client.put("/api/ai/settings", json={"max_active_cameras": 2, **candidate})
        self.assertEqual(activated.status_code, 200, activated.text)
        self.assertEqual(activated.json()["model"]["provider"], "GPU")
        self.assertEqual(activated.json()["model"]["model_version"], "sha256:fixture-1")
        with connect() as db:
            runtime = db.execute("SELECT provider,model_id,model_version,validated_at FROM ai_runtime_settings WHERE singleton_id=1").fetchone()
        self.assertEqual((runtime["provider"], runtime["model_id"], runtime["model_version"]), ("GPU", MODEL_ID, "sha256:fixture-1"))
        self.assertIsNotNone(runtime["validated_at"])

    def test_motion_recording_stream_keeps_writing_after_ai_is_stopped(self):
        test_stream = os.environ.get("NVR_RTSP_TEST_URL")
        if not test_stream:
            self.skipTest("CI simulated RTSP camera is required for the motion recording continuity check")
        self.active_user = {"id": 2, "username": "administrator", "role": "administrator"}
        with connect() as db:
            db.execute("UPDATE cameras SET stream_url_ciphertext=?,recording_mode='motion',recording_enabled=1,motion_fallback_enabled=1,motion_sensitivity=1,motion_post_roll_seconds=1 WHERE id=?",
                       (encrypt(test_stream), self.camera_id))
        policy = self.client.patch(f"/api/cameras/{self.camera_id}", json={"recording_mode": "motion", "recording_enabled": True})
        self.assertEqual(policy.status_code, 200, policy.text)
        deadline = time.monotonic() + 12
        before = None
        while time.monotonic() < deadline:
            with connect() as db:
                row = db.execute("SELECT recording_status,last_video_at FROM cameras WHERE id=?", (self.camera_id,)).fetchone()
            if row["recording_status"] in {"buffering", "degraded"} and row["last_video_at"]:
                before = row["last_video_at"]
                break
            time.sleep(0.1)
        self.assertIsNotNone(before, "motion recorder did not establish the simulated camera stream")

        class QuietDetector:
            def infer(self, frame, threshold):
                return []

        app.state.detectors.detector = QuietDetector()
        configured = self.client.put(f"/api/cameras/{self.camera_id}/ai", json={"enabled": True, "analysis_fps": 0.5, "use_substream": False})
        self.assertEqual(configured.status_code, 200, configured.text)
        stopped = self.client.put(f"/api/cameras/{self.camera_id}/ai", json={"enabled": False, "use_substream": False})
        self.assertEqual(stopped.status_code, 200, stopped.text)

        deadline = time.monotonic() + 8
        after = before
        while time.monotonic() < deadline:
            with connect() as db:
                row = db.execute("SELECT recording_status,last_video_at FROM cameras WHERE id=?", (self.camera_id,)).fetchone()
            after = row["last_video_at"]
            if after and after > before and row["recording_status"] in {"buffering", "degraded"}:
                break
            time.sleep(0.2)
        self.assertGreater(after or "", before, "motion recording freshness did not advance after AI shutdown")
        with connect() as db:
            camera = db.execute("SELECT recording_enabled,recording_mode FROM cameras WHERE id=?", (self.camera_id,)).fetchone()
        self.assertEqual((camera["recording_enabled"], camera["recording_mode"]), (1, "motion"))


if __name__ == "__main__":
    unittest.main()
