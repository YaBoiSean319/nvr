from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import administrator, app, current_user
from app.storage import camera_recordings_dir
from app.store import connect, initialize


class StorageApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.previous={key:os.environ.get(key) for key in ("NVR_DB_PATH","NVR_RECORDINGS_PATH","NVR_STORAGE_VOLUMES","NVR_STORAGE_PRESSURE_FREE_BYTES")}
        root=Path(self.temporary.name)/"recordings"
        (root/"archive").mkdir(parents=True)
        os.environ["NVR_DB_PATH"]=str(Path(self.temporary.name)/"state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"]=str(root)
        os.environ["NVR_STORAGE_VOLUMES"]=json.dumps({"default":"","archive":"archive"})
        os.environ["NVR_STORAGE_PRESSURE_FREE_BYTES"]="99999999999999"
        initialize()
        self.group=str(uuid.uuid4());self.camera=str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)",(self.group,"Storage group"))
            db.execute("INSERT INTO cameras(id,name,group_id,stream_url_ciphertext,recording_enabled) VALUES(?,?,?,X'01',1)",(self.camera,"Storage camera",self.group))
        app.dependency_overrides[current_user]=lambda:None
        app.dependency_overrides[administrator]=lambda:None
        self.context=TestClient(app);self.client=self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None,None,None)
        app.dependency_overrides.pop(current_user,None);app.dependency_overrides.pop(administrator,None)
        for key,value in self.previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
        self.temporary.cleanup()

    def test_admin_assigns_group_volume_and_sees_health_capacity_and_affected_cameras(self):
        result=self.client.put(f"/api/camera-groups/{self.group}/storage-volume",json={"volume_id":"archive"})
        self.assertEqual(result.status_code,200,result.text)
        with patch("app.storage.os.path.ismount",return_value=False):
            unavailable=self.client.get("/api/storage").json()
            archive=next(volume for volume in unavailable["volumes"] if volume["id"]=="archive")
            self.assertEqual(archive["state"],"missing")
            self.assertTrue(archive["retention_at_risk"])
        with patch("app.storage.os.path.ismount",return_value=True):
            response=self.client.get("/api/storage")
            self.assertEqual(response.status_code,200,response.text)
            archive=next(volume for volume in response.json()["volumes"] if volume["id"]=="archive")
            self.assertEqual(archive["state"],"pressure")
            self.assertIn("capacity_bytes",archive)
            self.assertTrue(archive["retention_at_risk"])
            self.assertEqual(result.json()["volume_id"],"archive")
            self.assertEqual(camera_recordings_dir(self.camera).parent,Path(os.environ["NVR_RECORDINGS_PATH"])/"archive")
        unassign=self.client.put(f"/api/camera-groups/{self.group}/storage-volume",json={"volume_id":"missing"})
        self.assertEqual(unassign.status_code,422)


if __name__=="__main__":
    unittest.main()
