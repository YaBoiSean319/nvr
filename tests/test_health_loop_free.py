"""The camera health pass stores what it found without stopping the background worker's event loop while the database is busy.

Every ten minutes the pass looks at each camera and stored the result with a connection opened on the background worker's event loop, one
camera after another; that loop also delivers webhooks and sends the worker's heartbeat. Each write waits for the database's lock, so a pass
over a fleet while another process held it stopped the worker for as long as that lasted."""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from tests.loop_stall import HOLD_SECONDS, longest_stall_while_locked

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")


class HealthLoopFreeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        from app.credentials import encrypt
        from app.store import connect, initialize

        initialize()
        self.camera_ids = [str(uuid.uuid4()) for _ in range(3)]
        with connect() as db:
            for number, camera_id in enumerate(self.camera_ids):
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_enabled) VALUES(?,?,?,1)",
                           (camera_id, f"Health {number}", encrypt("rtsp://camera.invalid/main")))

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def test_a_pass_over_the_fleet_waits_for_the_lock_without_stopping_the_loop(self):
        from app import camera_health
        from app.store import connect

        sampled: list[int] = []

        async def one_pass():
            sampled.append(await camera_health.sample_once())

        longest = longest_stall_while_locked(one_pass)
        self.assertLess(longest, HOLD_SECONDS / 2, "the background worker's event loop stopped while the health pass waited for the write lock")
        self.assertEqual(sampled, [3])
        with connect() as db:
            measured = db.execute("SELECT COUNT(*) FROM camera_health WHERE measured_at IS NOT NULL").fetchone()[0]
        self.assertEqual(measured, 3)  # Each camera's result was still stored once the lock was free


if __name__ == "__main__":
    unittest.main()
