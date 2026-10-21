from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import administrator, app, current_user
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
        app.dependency_overrides[current_user] = lambda: None
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
        self.assertIn("projected_days_at_current_activity",summary["retention_coverage"])
        self.assertEqual(self.client.delete(f"/api/cameras/{self.camera}/retention").status_code,204)
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera}/retention").json()["policy_source"],"group")
        self.assertEqual(self.client.delete(f"/api/camera-groups/{self.group}/retention").status_code,204)
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera}/retention").json()["policy_source"],"default")


if __name__ == "__main__":
    unittest.main()
