"""A test case with one AI-analysed camera and the detection fixture's footage.

``analyse_footage`` runs the real AI worker loop over the fixture footage, and can
run other workers (for example the rules worker) in the same event loop while it
does, the way they run side by side in production.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.application import create_app
from app.credentials import encrypt
from app.detection import DetectionManager
from app.store import connect
from tests.fixtures import detection_scene as scene
from tests.test_support import TEST_PASSWORD


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class SceneTestCase(unittest.TestCase):
    camera_name = "Loading dock"

    @classmethod
    def setUpClass(cls):
        cls.footage_directory = tempfile.TemporaryDirectory()
        cls.footage = scene.write_footage(Path(cls.footage_directory.name) / "scene.mp4")

    @classmethod
    def tearDownClass(cls):
        cls.footage_directory.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")}
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        (root / "recordings").mkdir()
        self.context = TestClient(create_app(start_workers=False))
        self.client = self.context.__enter__()
        created = self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD})
        self.assertEqual(created.status_code, 201, created.text)
        self.camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)",
                       (self.camera_id, self.camera_name, encrypt("rtsp://camera.invalid/main")))
            db.execute("INSERT INTO ai_camera_settings(camera_id,config_override,enabled,use_substream) VALUES(?,1,1,0)", (self.camera_id,))
        self.scene_start: datetime | None = None

    def tearDown(self):
        self.context.__exit__(None, None, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def analyse_footage(self, companions: Iterable[Any] = ()) -> None:
        """Run the AI worker's camera loop over the fixture footage until every frame is analysed.

        Each companion is a worker manager (``start``/``stop``) run alongside it and
        stopped only after the AI worker's last frame has been stored.
        """
        decoded = scene.decode_footage(self.footage)
        self.assertEqual(len(decoded), scene.FRAME_COUNT)
        times: list[datetime] = []

        def clock() -> datetime:
            # The first analysed frame is stamped at the real time, as in production;
            # later frames follow the scene's sampling interval.
            if not times:
                self.scene_start = datetime.now(timezone.utc)
                times.extend(scene.frame_times(self.scene_start))
            return times.pop(0)

        async def run() -> None:
            finished = asyncio.Event()

            async def footage_frames(source, fps):
                for item in decoded:
                    yield item
                finished.set()
                await asyncio.Event().wait()  # the camera stays connected without new frames

            manager = DetectionManager(clock=clock)
            manager.detector = scene.SceneDetector()
            others = list(companions)
            with patch("app.detection._raw_frames", footage_frames):
                for other in others:
                    await other.start()
                await manager.start()
                await asyncio.wait_for(finished.wait(), timeout=20)
                await asyncio.sleep(0.2)
                await manager.stop()
                for other in others:
                    await other.stop()

        asyncio.run(run())

    def move_scene_into_past(self, by: timedelta = timedelta(minutes=1)) -> None:
        """Shift the stored scene back in time, as though it was analysed ``by`` ago.

        The fixture clock stamps later frames up to the scene's length ahead of the real
        time; reads bounded by "now" need the whole scene behind them.
        """
        def shifted(value: str) -> str:
            return (datetime.fromisoformat(value) - by).isoformat()

        with connect() as db:
            for row in db.execute("SELECT id,detected_at FROM ai_detections").fetchall():
                db.execute("UPDATE ai_detections SET detected_at=? WHERE id=?", (shifted(row["detected_at"]), row["id"]))
            for row in db.execute("SELECT id,first_seen_at,last_seen_at FROM ai_tracks").fetchall():
                db.execute("UPDATE ai_tracks SET first_seen_at=?,last_seen_at=? WHERE id=?",
                           (shifted(row["first_seen_at"]), shifted(row["last_seen_at"]), row["id"]))
        self.scene_start -= by

    def scene_range(self) -> dict[str, str]:
        return {"camera_id": self.camera_id, "start": iso(self.scene_start - timedelta(minutes=1)),
                "end": iso(self.scene_start + timedelta(minutes=1))}
