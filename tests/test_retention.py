from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.retention import acquire_playback, cleanup_expired_recordings, effective_retention, release_playback
from app.store import connect, initialize


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("NVR_DB_PATH")
        self.old_recordings = os.environ.get("NVR_RECORDINGS_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.temporary.name) / "recordings")
        initialize()
        self.camera = str(uuid.uuid4())
        self.group = str(uuid.uuid4())
        Path(os.environ["NVR_RECORDINGS_PATH"]).mkdir()
        with connect() as db:
            db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)", (self.group, "Retention group"))
            db.execute("INSERT INTO cameras(id,name,group_id,stream_url_ciphertext) VALUES(?,?,?,X'01')", (self.camera,"Retention camera",self.group))

    def tearDown(self):
        for key, value in (("NVR_DB_PATH",self.old_db),("NVR_RECORDINGS_PATH",self.old_recordings)):
            if value is None: os.environ.pop(key,None)
            else: os.environ[key]=value
        self.temporary.cleanup()

    def test_default_group_and_camera_retention_inheritance(self):
        with connect() as db:
            self.assertEqual(effective_retention(db,self.camera),(30,"default"))
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('group',?,14)",(self.group,))
            self.assertEqual(effective_retention(db,self.camera),(14,"group"))
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,7)",(self.camera,))
            self.assertEqual(effective_retention(db,self.camera),(7,"camera"))

    def test_cleanup_removes_only_expired_cataloged_files_and_respects_playback(self):
        now=datetime.now(timezone.utc)
        old_end=(now-timedelta(days=40)).isoformat()
        old_start=(now-timedelta(days=40,minutes=1)).isoformat()
        recent_end=(now-timedelta(days=2)).isoformat()
        recent_start=(now-timedelta(days=2,minutes=1)).isoformat()
        root=Path(os.environ["NVR_RECORDINGS_PATH"])
        leased=root/self.camera/"leased.mp4"; leased.parent.mkdir(); leased.write_bytes(b"leased")
        removable=root/self.camera/"removable.mp4"; removable.write_bytes(b"remove")
        with connect() as db:
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,30)",(self.camera,))
            for name,start,end in (("leased.mp4",old_start,old_end),("removable.mp4",old_start,old_end),("missing.mp4",old_start,old_end),("recent.mp4",recent_start,recent_end)):
                db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",(self.camera,f"{self.camera}/{name}",start,end,10))
        acquire_playback(leased)
        try:
            result=cleanup_expired_recordings(now)
        finally:
            release_playback(leased)
        self.assertEqual(result["removed_segments"],2)
        self.assertEqual(result["skipped_playback"],1)
        self.assertTrue(leased.exists())
        self.assertFalse(removable.exists())
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recording_segments WHERE camera_id=?",(self.camera,)).fetchone()[0],2)
        # Once playback releases its lease, the old file is eligible for the next pass.
        self.assertEqual(cleanup_expired_recordings(now)["removed_segments"],1)


if __name__ == "__main__":
    unittest.main()
