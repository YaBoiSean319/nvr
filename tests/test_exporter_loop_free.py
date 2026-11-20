"""The export worker's database writes wait for the lock without stopping its event loop.

The export worker's loop renders one export at a time and sends the worker's heartbeat. Claiming a job, its progress, its failure or
cancellation, a redaction review's progress and end were written with connections opened on that loop: each waits for the database's
lock, and while it waited the loop stopped. A redaction review also fetched cold-tier footage (downloads from a bucket) on it. Each test
holds the lock from another connection and watches the loop keep turning (tests/loop_stall.py)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tests.loop_stall import HOLD_SECONDS, longest_stall_while_locked

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")


class ExporterLoopFreeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        (root / "recordings").mkdir()
        from app.credentials import encrypt
        from app.store import connect, initialize

        initialize()
        self.camera_id = str(uuid.uuid4())
        self.end = datetime.now(timezone.utc).replace(microsecond=0)
        self.start = self.end - timedelta(minutes=1)
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (self.camera_id, "Export", encrypt("rtsp://camera.invalid/main")))

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def test_an_export_is_claimed_and_its_failure_recorded_without_stopping_the_loop(self):
        from app.exports import ExportManager
        from app.store import connect

        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with connect() as db:  # A camera with no footage in the range: claimed, then failed
            db.execute("INSERT INTO exports(id,camera_ids_json,requested_start,requested_end,status,created_at,updated_at) VALUES(?,?,?,?,'queued',?,?)",
                       (job_id, json.dumps([self.camera_id]), self.start.isoformat(), self.end.isoformat(), now, now))

        async def run():
            await ExportManager()._execute(job_id)  # The job alone, without starting the worker's queue and poll

        longest = longest_stall_while_locked(run)
        self.assertLess(longest, HOLD_SECONDS / 2, "the export worker's event loop stopped while an export waited for the write lock")
        with connect() as db:
            row = db.execute("SELECT status,error FROM exports WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "failed", row["error"])  # Claimed and failed once the lock was free

    def test_a_redaction_review_is_claimed_and_its_end_recorded_without_stopping_the_loop(self):
        from app import redaction
        from app.store import connect

        review_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with connect() as db:
            db.execute("INSERT INTO redaction_reviews(id,camera_ids_json,requested_start,requested_end,kinds_json,state,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?)",
                       (review_id, json.dumps([self.camera_id]), self.start.isoformat(), self.end.isoformat(), json.dumps(["faces"]), now, now))

        with patch.object(redaction, "shared_detector", lambda: None):  # No models installed: the review ends at once, failed with a reason
            longest = longest_stall_while_locked(lambda: redaction.analyse(review_id))
        self.assertLess(longest, HOLD_SECONDS / 2, "the export worker's event loop stopped while a redaction review waited for the write lock")
        with connect() as db:
            row = db.execute("SELECT state,error FROM redaction_reviews WHERE id=?", (review_id,)).fetchone()
        self.assertEqual((row["state"], row["error"]), ("failed", "The redaction models are not installed"))


if __name__ == "__main__":
    unittest.main()
