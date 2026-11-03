"""Fall detection (#230) end to end: the setting, the AI worker over a scripted scene with a stand-in pose reader, the rule, the alarm and the API."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import falls, pose_models
from app.application import create_app
from app.migrations import m0047_falls
from app.store import connect
from tests.scene_case import SceneTestCase
from tests.test_falls import pose
from tests.test_support import TEST_PASSWORD


class ScriptedReader:
    """A pose reader that sees the red walker upright until it passes a quarter of the way across, then lying down; and the green walker always upright."""

    def __init__(self) -> None:
        self.calls = 0

    def pose(self, frame, box):
        self.calls += 1
        aspect = 16 / 9  # The camera's shape, as the worker uses
        centre = frame[int((box["y"] + box["height"] / 2) * frame.shape[0]), int((box["x"] + box["width"] / 2) * frame.shape[1])]
        red = int(centre[2]) > 150 and int(centre[1]) < 100
        return pose(85 if red and box["x"] > 0.25 else 5, aspect=aspect)


class WorkerTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        with connect() as db:
            db.execute("INSERT INTO fall_settings(camera_id,enabled,sensitivity,stay_down_seconds) VALUES(?,1,'high',2)", (self.camera_id,))

    def rule(self, **extra) -> str:
        response = self.client.post("/api/rules", json={"name": "Fall", "camera_ids": [self.camera_id], "condition": {"type": "fall"}, **extra})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_a_person_who_goes_down_and_stays_down_in_the_scene_raises_one_fall_and_one_alarm(self):
        self.rule()
        reader = ScriptedReader()
        with patch.object(pose_models, "load_reader", return_value=reader):
            self.analyse_footage()
        self.assertGreater(reader.calls, 20)
        with connect() as db:
            events = db.execute("SELECT * FROM fall_events").fetchall()
            alarms = db.execute("SELECT * FROM alarms").fetchall()
        self.assertEqual(len(events), 1)  # Only the red walker went down; the green one kept walking
        self.assertGreaterEqual(events[0]["down_seconds"], 2.0)
        self.assertEqual(json.loads(events[0]["box_json"]).keys(), {"x", "y", "width", "height"})
        self.assertEqual([(alarm["label"], alarm["condition_type"], alarm["camera_id"]) for alarm in alarms], [("fall", "fall", self.camera_id)])
        self.assertTrue(alarms[0]["track_id"])

    def test_nothing_is_looked_for_on_a_camera_with_fall_detection_off(self):
        with connect() as db:
            db.execute("UPDATE fall_settings SET enabled=0")
        reader = ScriptedReader()
        with patch.object(pose_models, "load_reader", return_value=reader):
            self.analyse_footage()
        self.assertEqual(reader.calls, 0)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fall_events").fetchone()[0], 0)

    def test_without_the_model_analysis_carries_on_and_no_falls_are_found(self):
        with patch.object(pose_models, "load_reader", return_value=None):
            self.analyse_footage()
        with connect() as db:
            self.assertGreater(db.execute("SELECT COUNT(*) FROM ai_detections").fetchone()[0], 0)  # Analysis still ran
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fall_events").fetchone()[0], 0)

    def test_a_pose_reader_that_fails_never_stops_analysis(self):
        class Broken:
            def pose(self, frame, box):
                raise RuntimeError("the model fell over")

        with patch.object(pose_models, "load_reader", return_value=Broken()):
            self.analyse_footage()
        with connect() as db:
            self.assertGreater(db.execute("SELECT COUNT(*) FROM ai_detections").fetchone()[0], 0)

    def test_a_fall_rule_for_another_camera_raises_no_alarm_but_the_fall_is_still_kept(self):
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('elsewhere','Elsewhere',X'01')")
        response = self.client.post("/api/rules", json={"name": "Fall elsewhere", "camera_ids": ["elsewhere"], "condition": {"type": "fall"}})
        self.assertEqual(response.status_code, 201, response.text)
        with patch.object(pose_models, "load_reader", return_value=ScriptedReader()):
            self.analyse_footage()
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fall_events").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM alarms").fetchone()[0], 0)

    def test_a_person_inside_a_privacy_mask_is_never_looked_at(self):
        with connect() as db:  # The whole picture is masked
            db.execute("INSERT INTO privacy_masks(camera_id,zones_json,camera_side,updated_at) VALUES(?,?,'none','2026-11-03T00:00:00+00:00')",
                       (self.camera_id, json.dumps([{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}])))
        reader = ScriptedReader()
        with patch.object(pose_models, "load_reader", return_value=reader):
            self.analyse_footage()
        self.assertEqual(reader.calls, 0)  # No person's picture is given to the model
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fall_events").fetchone()[0], 0)

    def test_a_rule_needs_a_camera_or_group(self):
        response = self.client.post("/api/rules", json={"name": "Nowhere", "camera_ids": [], "group_ids": [], "condition": {"type": "fall"}})
        self.assertEqual(response.status_code, 422)


class ApiTests(unittest.TestCase):
    def setUp(self):
        import os
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_POSE_MODELS_DIR")}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.temporary.name) / "credentials.key")
        self.context = TestClient(create_app(start_workers=False))
        self.client = self.context.__enter__()
        self.assertEqual(self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 201)
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('c1','Lounge',X'01')")

    def tearDown(self):
        import os
        self.context.__exit__(None, None, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def test_the_page_lists_cameras_with_their_defaults_and_the_models_state(self):
        body = self.client.get("/api/admin/falls").json()
        self.assertEqual(body["defaults"], {"sensitivity": "high", "stay_down_seconds": 8.0, "least_analysis_fps": 1.0})
        self.assertEqual(body["cameras"], [{"id": "c1", "name": "Lounge", "enabled": False, "sensitivity": "high", "stay_down_seconds": 8.0, "analysis_fps": None, "analysis_on": False}])
        self.assertIn("Apache-2.0", body["model"]["licence"])
        self.assertEqual(body["recent"], [])

    def test_a_camera_is_turned_on_with_its_settings_and_the_change_is_audited(self):
        with patch.object(pose_models, "installed", return_value=True):
            saved = self.client.put("/api/admin/falls/cameras/c1", json={"enabled": True, "sensitivity": "normal", "stay_down_seconds": 5})
        self.assertEqual(saved.status_code, 200, saved.text)
        with connect() as db:
            self.assertEqual(falls.fall_settings(db, "c1"), (True, "normal", 5.0))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='falls_camera_changed'").fetchone()[0], 1)

    def test_a_camera_nobody_would_be_alerted_about_is_flagged_until_an_enabled_rule_covers_it(self):
        with patch.object(pose_models, "installed", return_value=True):
            self.client.put("/api/admin/falls/cameras/c1", json={"enabled": True})
        self.assertEqual(self.client.get("/api/admin/falls").json()["unalerted_camera_ids"], ["c1"])
        made = self.client.post("/api/rules", json={"name": "Person fell", "camera_ids": ["c1"], "condition": {"type": "fall"}})
        self.assertEqual(made.status_code, 201, made.text)
        self.assertEqual(self.client.get("/api/admin/falls").json()["unalerted_camera_ids"], [])
        self.client.put(f"/api/rules/{made.json()['id']}", json={**{key: made.json()[key] for key in ("name", "camera_ids", "group_ids", "condition", "classes", "schedule", "cooldown_seconds", "pop_into_live")}, "enabled": False, "actions": []})
        self.assertEqual(self.client.get("/api/admin/falls").json()["unalerted_camera_ids"], ["c1"])  # A disabled rule alerts nobody

    def test_the_analysis_rate_of_a_watched_camera_is_reported_so_a_slow_one_can_be_flagged(self):
        with patch.object(pose_models, "installed", return_value=True):
            self.client.put("/api/admin/falls/cameras/c1", json={"enabled": True})
        with connect() as db:
            db.execute("INSERT INTO ai_camera_settings(camera_id,config_override,enabled,analysis_fps) VALUES('c1',1,1,0.5)")
        camera = self.client.get("/api/admin/falls").json()["cameras"][0]
        self.assertEqual((camera["analysis_on"], camera["analysis_fps"]), (True, 0.5))
        with connect() as db:
            db.execute("UPDATE ai_camera_settings SET enabled=0")
        camera = self.client.get("/api/admin/falls").json()["cameras"][0]
        self.assertEqual((camera["analysis_on"], camera["analysis_fps"]), (False, None))  # AI analysis is off, so nothing is looked at at all

    def test_it_cannot_be_turned_on_without_the_model_and_bad_values_are_refused(self):
        with patch.object(pose_models, "installed", return_value=False):
            refused = self.client.put("/api/admin/falls/cameras/c1", json={"enabled": True})
            off = self.client.put("/api/admin/falls/cameras/c1", json={"enabled": False})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(off.status_code, 200)  # Turning it off never needs the model
        self.assertEqual(self.client.put("/api/admin/falls/cameras/c1", json={"enabled": False, "stay_down_seconds": 0.2}).status_code, 422)
        self.assertEqual(self.client.put("/api/admin/falls/cameras/c1", json={"enabled": False, "sensitivity": "extreme"}).status_code, 422)
        self.assertEqual(self.client.put("/api/admin/falls/cameras/nowhere", json={"enabled": False}).status_code, 404)

    def test_only_administrators_see_or_change_it(self):
        anonymous = TestClient(self.client.app)
        self.assertEqual(anonymous.get("/api/admin/falls").status_code, 401)
        self.assertEqual(anonymous.put("/api/admin/falls/cameras/c1", json={"enabled": False}).status_code, 401)

    def test_a_fall_is_kept_with_the_camera_and_goes_with_its_footage(self):
        from datetime import datetime, timezone
        at = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        with connect() as db:
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES('c1','c1/a.mp4',?,?,10)", ("2026-11-03T11:59:00+00:00", "2026-11-03T12:01:00+00:00"))
        falls.store_fall("c1", "track-1", at, 3.4, {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4})
        self.assertEqual([row["camera_name"] for row in self.client.get("/api/admin/falls").json()["recent"]], ["Lounge"])
        with connect() as db:
            db.execute("DELETE FROM recording_segments")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fall_events").fetchone()[0], 0)  # The footage went, so did the record of the fall


class MigrationTests(unittest.TestCase):
    def test_the_rules_table_accepts_the_fall_condition_after_the_upgrade(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            previous = os.environ.get("NVR_DB_PATH")
            os.environ["NVR_DB_PATH"] = str(Path(folder) / "state.sqlite3")
            try:
                from app.store import initialize
                initialize()
                with connect() as db:
                    m0047_falls.verify(db)
                    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rules'").fetchone()[0]
                    self.assertIn("'plate','fall'", sql)
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute("INSERT INTO rules(id,name,enabled,camera_ids_json,group_ids_json,condition_type,condition_json,classes_json) VALUES('x','x',1,'[]','[]','nonsense','{}','[]')")
            finally:
                if previous is None:
                    os.environ.pop("NVR_DB_PATH", None)
                else:
                    os.environ["NVR_DB_PATH"] = previous


if __name__ == "__main__":
    unittest.main()
