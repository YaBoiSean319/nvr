from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.principal import administrator, current_user
from tests.test_support import principal
from app.store import connect, initialize


class RetentionApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("NVR_DB_PATH")
        self.old_recordings = os.environ.get("NVR_RECORDINGS_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.temporary.name) / "recordings")
        initialize()
        Path(os.environ["NVR_RECORDINGS_PATH"]).mkdir()
        self.group = str(uuid.uuid4())
        self.camera = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)", (self.group,"API group"))
            db.execute("INSERT INTO cameras(id,name,group_id,stream_url_ciphertext) VALUES(?,?,?,X'01')", (self.camera,"API camera",self.group))
        app.dependency_overrides[current_user] = lambda: principal(1, "tester", "administrator")
        app.dependency_overrides[administrator] = lambda: None
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None,None,None)
        app.dependency_overrides.pop(current_user,None)
        app.dependency_overrides.pop(administrator,None)
        for key,value in (("NVR_DB_PATH",self.old_db),("NVR_RECORDINGS_PATH",self.old_recordings)):
            if value is None: os.environ.pop(key,None)
            else: os.environ[key]=value
        self.temporary.cleanup()

    def test_admin_group_and_camera_retention_controls_and_achieved_summary(self):
        group=self.client.put(f"/api/camera-groups/{self.group}/retention",json={"retention_days":14})
        self.assertEqual(group.status_code,200,group.text)
        inherited=self.client.get(f"/api/cameras/{self.camera}/retention").json()
        self.assertEqual(inherited["retention_days"],14)
        self.assertEqual(inherited["policy_source"],"group")
        camera=self.client.put(f"/api/cameras/{self.camera}/retention",json={"retention_days":7})
        self.assertEqual(camera.status_code,200,camera.text)
        self.assertEqual(camera.json()["policy_source"],"camera")
        summary=self.client.get("/api/cameras").json()["cameras"][0]
        self.assertEqual(summary["retention_days"],7)
        self.assertEqual(summary["retention_policy_source"],"camera")
        # The footage summary reads every segment, so it is on the camera's own endpoint, never in the list that is refreshed every 15 s (#277).
        self.assertNotIn("retention_coverage",summary)
        self.assertIn("projected_days_at_current_activity",self.client.get(f"/api/cameras/{self.camera}/retention").json())
        self.assertEqual(self.client.delete(f"/api/cameras/{self.camera}/retention").status_code,204)
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera}/retention").json()["policy_source"],"group")
        self.assertEqual(self.client.delete(f"/api/camera-groups/{self.group}/retention").status_code,204)
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera}/retention").json()["policy_source"],"default")

    def test_the_camera_list_does_not_read_a_cameras_footage_index(self):
        # The list is refreshed every 15 s in every open tab; reading every segment of every camera for it took 1.6 s with 16 cameras
        # and 30 days (#277). Count the statements that touch the segment index while the list is built.
        with connect() as db:
            db.executemany("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,1)",
                           [(self.camera,f"s{minute}.mp4",f"2026-01-01T00:{minute:02d}:00+00:00",f"2026-01-01T00:{minute:02d}:59+00:00") for minute in range(50)])
        seen=[]
        real=sqlite3.connect
        def watching(*args,**kwargs):
            connection=real(*args,**kwargs)
            connection.set_trace_callback(lambda statement: seen.append(statement) if "recording_segments" in statement else None)
            return connection
        with patch("sqlite3.connect",watching):
            self.assertEqual(self.client.get("/api/cameras").status_code,200)
        self.assertEqual(seen,[])


if __name__ == "__main__":
    unittest.main()
