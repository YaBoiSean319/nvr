from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from fastapi import HTTPException

from app.main import administrator, app, current_user
from app.retention import cleanup_expired_recordings
from app.store import connect, initialize


class PreservationHoldTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.previous={key:os.environ.get(key) for key in ("NVR_DB_PATH","NVR_RECORDINGS_PATH","NVR_STORAGE_VOLUMES")}
        self.root=Path(self.temporary.name)/"recordings"
        self.root.mkdir()
        os.environ["NVR_DB_PATH"]=str(Path(self.temporary.name)/"state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"]=str(self.root)
        os.environ.pop("NVR_STORAGE_VOLUMES",None)
        initialize()
        self.camera=str(uuid.uuid4());self.segment_id=None
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('operator','unused','operator')")
            self.operator_id=db.execute("SELECT id FROM users WHERE username='operator'").fetchone()[0]
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('administrator','unused','administrator')")
            self.admin_id=db.execute("SELECT id FROM users WHERE username='administrator'").fetchone()[0]
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')",(self.camera,"Held camera"))
            started=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat();ended=(datetime.now(timezone.utc)-timedelta(days=10,minutes=-1)).isoformat()
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",(self.camera,f"{self.camera}/segment.mp4",started,ended,256))
            self.segment_id=db.execute("SELECT id FROM recording_segments WHERE camera_id=?",(self.camera,)).fetchone()[0]
        self.media=self.root/self.camera/"segment.mp4";self.media.parent.mkdir();self.media.write_bytes(b"footage"*100)
        self.active_user={"id":self.operator_id,"role":"operator"}
        app.dependency_overrides[current_user]=lambda:self.active_user
        def admin_override():
            if self.active_user["role"]!="administrator": raise HTTPException(status_code=403,detail="administrator role required")
            return self.active_user
        app.dependency_overrides[administrator]=admin_override
        self.context=TestClient(app);self.client=self.context.__enter__()
        with connect() as db:
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,1)",(self.camera,))

    def tearDown(self):
        self.context.__exit__(None,None,None)
        app.dependency_overrides.pop(current_user,None);app.dependency_overrides.pop(administrator,None)
        for key,value in self.previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
        self.temporary.cleanup()

    def test_operator_hold_blocks_cleanup_and_delete_until_admin_release_then_audits(self):
        url=f"/api/cameras/{self.camera}/segments/{self.segment_id}"
        with connect() as db:
            self.assertIsNotNone(
                db.execute("SELECT 1 FROM recording_segments WHERE id=? AND camera_id=?", (self.segment_id, self.camera)).fetchone(),
                "the test interval must still exist after application startup",
            )
        denied=self.client.post(url+"/hold",json={"expires_in_days":3})
        self.assertEqual(denied.status_code,403)
        held=self.client.post(url+"/hold",json={})
        self.assertEqual(held.status_code,201,held.text)
        self.assertIsNone(held.json()["expires_at"])
        self.assertEqual(self.client.delete(url).status_code,403)
        self.assertEqual(cleanup_expired_recordings()["removed_segments"],0)
        self.assertTrue(self.media.exists())
        self.active_user={"id":self.admin_id,"role":"administrator"}
        self.assertEqual(self.client.delete(url).status_code,409)
        released=self.client.delete(url+"/hold")
        self.assertEqual(released.status_code,204)
        expiring=self.client.post(url+"/hold",json={"expires_in_days":2})
        self.assertEqual(expiring.status_code,201,expiring.text)
        listed=self.client.get(f"/api/cameras/{self.camera}/segments").json()["segments"][0]
        self.assertTrue(listed["preserved"])
        self.assertIsNotNone(listed["hold_expires_at"])
        self.assertEqual(self.client.delete(url).status_code,409)
        self.assertEqual(self.client.delete(url+"/hold").status_code,204)
        self.assertEqual(self.client.delete(url).status_code,204)
        self.assertFalse(self.media.exists())
        with connect() as db:
            actions=[row[0] for row in db.execute("SELECT action FROM audit_log ORDER BY id")]
        self.assertEqual(actions,["preserve","release_hold","preserve","release_hold","delete"])


if __name__=="__main__":
    unittest.main()
