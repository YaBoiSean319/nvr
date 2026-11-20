"""A PTZ tour's step and a camera's privacy masks are saved without stopping the event loop while the database is busy.

A tour step's state was written on the background worker's event loop after each move, and saving privacy masks wrote its transaction on
the web server's: each waits for the database's lock, and while it waited the loop (every other request, webhook deliveries, the worker's
heartbeat) stopped. Each test holds the lock from another connection and watches the loop keep turning (tests/loop_stall.py)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tests.loop_stall import HOLD_SECONDS, longest_stall_while_locked

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")


class PtzPrivacyLoopFreeTests(unittest.TestCase):
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
        self.camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (self.camera_id, "Yard", encrypt("rtsp://camera.invalid/main")))
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def test_a_tour_step_is_saved_without_stopping_the_loop(self):
        from app import ptz
        from app.store import connect

        tour_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO ptz_tours(id,camera_id,name,steps_json) VALUES(?,?,?,?)", (tour_id, self.camera_id, "Round", json.dumps([])))
            updated_at = db.execute("SELECT updated_at FROM ptz_tours WHERE id=?", (tour_id,)).fetchone()[0]
        move = ptz._TourMove(tour_id, updated_at, self.camera_id, "preset-1", 0, 1, 10.0)  # A camera with no PTZ: the step is tried again shortly

        longest = longest_stall_while_locked(lambda: ptz._run_move(move, datetime.now(timezone.utc)))
        self.assertLess(longest, HOLD_SECONDS / 2, "the background worker's event loop stopped while a tour step waited for the write lock")
        with connect() as db:
            state = db.execute("SELECT step,state FROM ptz_tour_state WHERE tour_id=?", (tour_id,)).fetchone()
        self.assertEqual((state["step"], state["state"]), (0, "running"))  # Saved once the lock was free

    def test_saving_privacy_masks_does_not_stop_the_loop(self):
        from app import privacy
        from app.store import connect

        zones = [{"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}]
        saved: list = []

        async def save():
            saved.append(await privacy.save(self.camera_id, zones, 1))

        longest = longest_stall_while_locked(save)
        self.assertLess(longest, HOLD_SECONDS / 2, "the web server's event loop stopped while saving masks waited for the write lock")
        with connect() as db:
            stored = db.execute("SELECT zones_json FROM privacy_masks WHERE camera_id=?", (self.camera_id,)).fetchone()
        self.assertEqual(json.loads(stored["zones_json"]), zones)
        self.assertTrue(saved and saved[0])


if __name__ == "__main__":
    unittest.main()
