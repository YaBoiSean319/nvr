from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.application import create_app
from app.credentials import encrypt
from app.detection import DetectionManager
from app.principal import COOKIE_NAME
from app.store import connect
from tests.fixtures import detection_scene as scene
from tests.test_support import TEST_PASSWORD


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class DetectionTrackTests(unittest.TestCase):
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
                       (self.camera_id, "Loading dock", encrypt("rtsp://camera.invalid/main")))
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

    def _analyse_footage(self) -> None:
        """Run the AI worker's camera loop over the fixture footage until every frame is analysed."""
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
            with patch("app.detection._raw_frames", footage_frames):
                await manager.start()
                await asyncio.wait_for(finished.wait(), timeout=20)
                await asyncio.sleep(0.2)
                await manager.stop()

        asyncio.run(run())

    def _range(self) -> dict[str, str]:
        return {"camera_id": self.camera_id, "start": _iso(self.scene_start - timedelta(minutes=1)),
                "end": _iso(self.scene_start + timedelta(minutes=1))}

    def test_worker_stores_detections_with_boxes_linked_into_tracks(self):
        self._analyse_footage()

        tracks = self.client.get("/api/ai/tracks", params=self._range())
        self.assertEqual(tracks.status_code, 200, tracks.text)
        listed = tracks.json()["tracks"]
        self.assertEqual(sorted(track["label"] for track in listed), ["person", "person", "vehicle"])
        self.assertTrue(all(track["detection_count"] == scene.FRAME_COUNT for track in listed), listed)

        colors = []
        for track in listed:
            detail = self.client.get(f"/api/ai/tracks/{track['id']}")
            self.assertEqual(detail.status_code, 200, detail.text)
            detections = detail.json()["detections"]
            self.assertEqual(len(detections), scene.FRAME_COUNT)
            # The crossing people never swap tracks: every detection on a track has one colour.
            track_colors = {item["attributes"]["color"] for item in detections}
            self.assertEqual(len(track_colors), 1, track_colors)
            colors.append(track_colors.pop())
            self.assertEqual(detail.json()["first_seen_at"], detections[0]["detected_at"])
            self.assertEqual(detail.json()["last_seen_at"], detections[-1]["detected_at"])
        self.assertEqual(sorted(colors), ["blue", "green", "red"])

        found = self.client.get("/api/ai/detections", params={**self._range(), "label": "vehicle"})
        self.assertEqual(found.status_code, 200, found.text)
        vehicles = found.json()["detections"]
        self.assertEqual(len(vehicles), scene.FRAME_COUNT)
        first = vehicles[0]
        # The first vehicle box is 90x40 pixels at (350,170) in a 448x256 frame.
        self.assertAlmostEqual(first["box"]["x"], 350 / 448, delta=0.01)
        self.assertAlmostEqual(first["box"]["y"], 170 / 256, delta=0.01)
        self.assertAlmostEqual(first["box"]["width"], 90 / 448, delta=0.01)
        self.assertAlmostEqual(first["box"]["height"], 40 / 256, delta=0.01)
        self.assertEqual(_parse(first["detected_at"]), self.scene_start)
        self.assertEqual(first["confidence"], 0.9)
        self.assertIn("model_id", first)

    def test_unanalysed_intervals_are_reported_and_never_contain_detections(self):
        self._analyse_footage()
        # The camera then stops being analysed: what follows is unknown, not empty.
        with connect() as db:
            db.execute("UPDATE ai_camera_settings SET enabled=0 WHERE camera_id=?", (self.camera_id,))
        from app.detection import set_camera_state
        set_camera_state(self.camera_id, "failed", "Camera stream ended", at=self.scene_start + timedelta(seconds=20))

        found = self.client.get("/api/ai/detections", params=self._range())
        self.assertEqual(found.status_code, 200, found.text)
        body = found.json()
        self.assertEqual(len(body["detections"]), scene.FRAME_COUNT * len(scene.OBJECTS))
        states = [interval["state"] for interval in body["unanalyzed"]]
        self.assertIn("unanalyzed", states)  # while the worker was connecting
        self.assertIn("failed", states)
        for interval in body["unanalyzed"]:
            starts = _parse(interval["starts_at"])
            ends = _parse(interval["ends_at"]) if interval["ends_at"] else datetime.max.replace(tzinfo=timezone.utc)
            inside = [item for item in body["detections"] if starts <= _parse(item["detected_at"]) < ends]
            self.assertEqual(inside, [], interval)

        # The existing timeline keeps its shape: observation intervals and unanalysed coverage.
        span = self._range()
        timeline = self.client.post("/api/playback/timeline", json={"camera_ids": [self.camera_id], "start": span["start"], "end": span["end"]})
        self.assertEqual(timeline.status_code, 200, timeline.text)
        camera = timeline.json()["cameras"][0]
        self.assertEqual({"id", "label", "confidence", "first_seen_at", "last_seen_at"}, set(camera["ai_detections"][0]))
        self.assertEqual(len(camera["ai_detections"]), len(scene.OBJECTS))
        self.assertEqual({"state", "starts_at", "ends_at", "detail"}, set(camera["ai_unanalyzed"][0]))

    def _add_segment(self, starts_at: datetime, ends_at: datetime) -> int:
        name = f"{self.camera_id}/segment-{starts_at:%H%M%S%f}.mp4"
        path = Path(os.environ["NVR_RECORDINGS_PATH"]) / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"footage")
        with connect() as db:
            return db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                              (self.camera_id, name, starts_at.isoformat(), ends_at.isoformat(), 7)).lastrowid

    def _tracks(self) -> list[dict]:
        response = self.client.get("/api/ai/tracks", params=self._range())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["tracks"]

    def test_deleting_footage_removes_the_detections_it_covers(self):
        self._analyse_footage()
        middle = self.scene_start + scene.FRAME_INTERVAL * 15
        first = self._add_segment(self.scene_start, middle)
        self._add_segment(middle, self.scene_start + scene.FRAME_INTERVAL * scene.FRAME_COUNT)

        deleted = self.client.delete(f"/api/cameras/{self.camera_id}/segments/{first}")
        self.assertEqual(deleted.status_code, 204, deleted.text)

        remaining = self.client.get("/api/ai/detections", params=self._range()).json()["detections"]
        self.assertEqual(len(remaining), 15 * len(scene.OBJECTS))
        self.assertTrue(all(_parse(item["detected_at"]) >= middle for item in remaining))
        tracks = self._tracks()
        self.assertEqual(len(tracks), len(scene.OBJECTS))
        for track in tracks:
            self.assertEqual(track["detection_count"], 15)
            self.assertEqual(_parse(track["first_seen_at"]), middle)
        detail = self.client.get(f"/api/ai/tracks/{tracks[0]['id']}").json()
        self.assertEqual(len(detail["detections"]), 15)

    def test_tracking_continues_after_footage_under_an_active_track_is_deleted(self):
        from app.tracks import Tracker, record_detections

        detector, tracker = scene.SceneDetector(), Tracker()
        start = datetime.now(timezone.utc)
        times = scene.frame_times(start)
        model = {"model_id": "fixture", "model_version": "1", "provider": "CPU"}
        segment = self._add_segment(start, times[10])
        for index in range(10):
            with connect() as db:
                record_detections(db, self.camera_id, detector.infer(scene.frame(index), 0.5), times[index], tracker, frame=scene.frame(index), **model)
        self.assertEqual(self.client.delete(f"/api/cameras/{self.camera_id}/segments/{segment}").status_code, 204)
        for index in range(10, 20):
            with connect() as db:
                record_detections(db, self.camera_id, detector.infer(scene.frame(index), 0.5), times[index], tracker, frame=scene.frame(index), **model)
        self.scene_start = start
        tracks = self._tracks()
        self.assertEqual(len(tracks), len(scene.OBJECTS))
        self.assertTrue(all(track["detection_count"] == 10 for track in tracks), tracks)

    def test_deleting_an_archived_cameras_footage_removes_all_its_detections(self):
        self._analyse_footage()
        middle = self.scene_start + scene.FRAME_INTERVAL * 15
        self._add_segment(middle, middle + scene.FRAME_INTERVAL * 2)  # motion clip; the rest is uncovered
        with connect() as db:
            db.execute("UPDATE cameras SET archived_at=?,enabled=0 WHERE id=?", (_iso(datetime.now(timezone.utc)), self.camera_id))
        deleted = self.client.request("DELETE", f"/api/cameras/{self.camera_id}/footage", json={"confirm_camera_name": "Loading dock"})
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertEqual(self._tracks(), [])

    def test_retention_expiry_removes_detections_and_tracks(self):
        from app.retention import cleanup_expired_recordings

        self._analyse_footage()
        end = self.scene_start + scene.FRAME_INTERVAL * scene.FRAME_COUNT
        self._add_segment(self.scene_start, end)
        with connect() as db:
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,7)", (self.camera_id,))

        self.assertEqual(cleanup_expired_recordings(end + timedelta(days=6))["removed_segments"], 0)
        self.assertEqual(len(self._tracks()), len(scene.OBJECTS))

        self.assertEqual(cleanup_expired_recordings(end + timedelta(days=8))["removed_segments"], 1)
        self.assertEqual(self._tracks(), [])
        self.assertEqual(self.client.get("/api/ai/detections", params=self._range()).json()["detections"], [])

    def test_detection_reads_need_a_signed_in_user_or_a_read_token(self):
        self._analyse_footage()
        track_id = self._tracks()[0]["id"]
        paths = [("/api/ai/detections", self._range()), ("/api/ai/tracks", self._range()), (f"/api/ai/tracks/{track_id}", None)]

        def token(*scopes: str) -> str:
            response = self.client.post("/api/admin/api-tokens", json={"name": "Detections", "scopes": list(scopes)})
            self.assertEqual(response.status_code, 201, response.text)
            return response.json()["secret"]

        read_token, media_token, export_token = token("read"), token("media"), token("export")
        with connect() as db:
            from app.security import hash_password
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", ("watcher", hash_password(TEST_PASSWORD), "operator"))
        with TestClient(self.client.app) as anonymous, TestClient(self.client.app) as operator:
            signed_in = operator.post("/api/auth/login", json={"username": "watcher", "password": TEST_PASSWORD})
            self.assertEqual(signed_in.status_code, 200, signed_in.text)
            self.assertIsNotNone(operator.cookies.get(COOKIE_NAME))
            for path, params in paths:
                self.assertEqual(anonymous.get(path, params=params).status_code, 401, path)
                self.assertEqual(operator.get(path, params=params).status_code, 200, path)
                for secret, expected in ((read_token, 200), (media_token, 403), (export_token, 403)):
                    response = anonymous.get(path, params=params, headers={"Authorization": f"Bearer {secret}"})
                    self.assertEqual(response.status_code, expected, (path, response.text))
                # Like other footage reads, tokens are never taken from the query string.
                self.assertEqual(anonymous.get(path, params={**(params or {}), "token": read_token}).status_code, 401, path)

    def test_retention_expires_detections_recorded_without_footage(self):
        from app.retention import cleanup_expired_recordings

        # Motion-only or paused recording: the camera was analysed but nothing was written.
        self._analyse_footage()
        end = self.scene_start + scene.FRAME_INTERVAL * scene.FRAME_COUNT
        cleanup_expired_recordings(end + timedelta(days=29))
        self.assertEqual(len(self._tracks()), len(scene.OBJECTS))
        cleanup_expired_recordings(end + timedelta(days=31))
        self.assertEqual(self._tracks(), [])


if __name__ == "__main__":
    unittest.main()
