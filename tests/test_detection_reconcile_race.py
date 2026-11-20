"""A camera's AI settings saved while the reconcile pass is under way are not undone by that pass's older reading of them.

Every few seconds the AI worker's reconcile pass reads every camera's settings (its survey), then settles the cameras one at a time. A
save that came in after the survey read a camera's settings, and settled that camera at once, was then undone when the pass reached the
camera with what it had read before: switching analysis on was switched off again ("disabled") until the next pass, and the other way
round. CI caught it in tests/test_detection.py, where enabling a camera answered "disabled".
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from app.credentials import encrypt
from app.detection import DetectionManager
from app.store import connect, initialize


class ReconcileRaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")}
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        (root / "recordings").mkdir()
        initialize()
        self.camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (self.camera_id, "Gate", encrypt("rtsp://camera.invalid/main")))
            db.execute("INSERT INTO ai_camera_settings(camera_id,config_override,enabled,use_substream) VALUES(?,1,?,0)", (self.camera_id, self.initially_enabled))

    initially_enabled = 0

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def set_enabled(self, enabled: bool) -> None:
        with connect() as db:
            db.execute("UPDATE ai_camera_settings SET enabled=? WHERE camera_id=?", (int(enabled), self.camera_id))

    def interleave(self, change) -> tuple[DetectionManager, list[tuple[str, str | None]]]:
        """Run a reconcile pass whose survey reads the settings before ``change`` saves new ones and settles the camera directly."""
        states: list[tuple[str, str | None]] = []

        async def run() -> DetectionManager:
            manager = DetectionManager()
            manager.detector = object()  # Any model will do: the camera's analysis below never reads a frame

            async def analysis(camera_id, config, detector):
                await asyncio.Event().wait()  # Analysing until it is stopped

            async def record(camera_id, state, error=None):
                states.append((state, error))

            manager._run, manager._state = analysis, record
            surveyed, release = threading.Event(), threading.Event()
            survey = manager._survey

            def slow_survey():
                result = survey()  # The settings as they are now
                surveyed.set()
                release.wait(10)  # ...and the pass goes on with them only after the new settings were saved and applied
                return result

            manager._survey = slow_survey
            if self.initially_enabled:
                await manager.sync_camera(self.camera_id)
            passing = asyncio.create_task(manager.reconcile())
            await asyncio.to_thread(surveyed.wait, 10)
            await change(manager)
            release.set()
            await passing
            running = manager.tasks.get(self.camera_id)
            alive = running is not None and not running.done()
            manager.alive = alive
            await manager.stop()
            return manager

        return asyncio.run(run()), states

    def test_switching_analysis_on_during_a_pass_is_not_switched_off_by_it(self):
        async def enable(manager):
            self.set_enabled(True)
            await manager.sync_camera(self.camera_id, force_restart=True)  # What saving the camera's AI settings does

        manager, states = self.interleave(enable)
        self.assertTrue(manager.alive, "the pass stopped the analysis that had just been switched on")
        self.assertEqual(states[-1][0], "connecting", states)


class ReconcileRaceOffTests(ReconcileRaceTests):
    initially_enabled = 1

    def test_switching_analysis_off_during_a_pass_is_not_switched_back_on_by_it(self):
        async def disable(manager):
            self.set_enabled(False)
            await manager.sync_camera(self.camera_id, force_restart=True)

        manager, states = self.interleave(disable)
        self.assertFalse(manager.alive, "the pass started again the analysis that had just been switched off")
        self.assertEqual(states[-1][0], "disabled", states)

    test_switching_analysis_on_during_a_pass_is_not_switched_off_by_it = None  # type: ignore[assignment]  # Only the class above starts disabled


if __name__ == "__main__":
    unittest.main()
