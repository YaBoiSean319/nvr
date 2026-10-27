"""Export redaction (#130): faces and plates are blurred in the exported frames, reviewed first, and recorded in the package."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from app import redaction
from app.main import app
from app.principal import current_user
from app.redaction import FRAME_HEIGHT, FRAME_WIDTH, RedactionDetector, SsdModel
from app.store import connect, initialize
from tests.test_access_grants import START as START_TIME, GrantCase
from tests.test_support import principal

FFMPEG = shutil.which("ffmpeg")
START, END = "2026-10-20T10:00:00Z", "2026-10-20T10:00:03Z"

# Where the fixture's faces and plate are (pixels of the 1280x720 frame): one subject to blur, one to keep, one plate.
FACE_A = (100, 100, 80, 80)
FACE_B = (900, 300, 80, 80)
PLATE = (500, 600, 200, 60)


def solid(mask: np.ndarray, size: int = 6) -> np.ndarray:
    """The pixels inside a size-by-size block that is entirely set: shapes survive, noise that happens to look red does not."""
    total = np.pad(mask.astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    window = total[size:, size:] - total[:-size, size:] - total[size:, :-size] + total[:-size, :-size]
    full = np.zeros(mask.shape, dtype=bool)
    full[:window.shape[0], :window.shape[1]] = window == size * size
    out = np.zeros_like(full)
    for dy in range(size):
        for dx in range(size):
            out[dy:, dx:] |= full[:full.shape[0] - dy, :full.shape[1] - dx]
    return out


def synthetic_detections(image: np.ndarray, kinds) -> list[dict]:
    """A stand-in detector: red squares are faces and yellow rectangles are plates (the models themselves are the administrator's)."""
    blue, green, red = (image[..., index].astype(int) for index in range(3))
    found = []
    for kind, mask in (("face", (red > 170) & (green < 110) & (blue < 110)), ("plate", (red > 170) & (green > 170) & (blue < 110))):
        if kind not in kinds:
            continue
        mask = solid(mask)
        columns = np.flatnonzero(mask.any(axis=0))
        if not columns.size:
            continue
        for run in np.split(columns, np.flatnonzero(np.diff(columns) > 1) + 1):
            rows = np.flatnonzero(mask[:, run[0]:run[-1] + 1].any(axis=1))
            found.append({"kind": kind, "confidence": 0.9, "box": {"x": run[0] / FRAME_WIDTH, "y": rows[0] / FRAME_HEIGHT,
                                                                   "width": (run[-1] - run[0] + 1) / FRAME_WIDTH, "height": (rows[-1] - rows[0] + 1) / FRAME_HEIGHT}})
    return found


class StandIn:
    model_id = "redaction:test"

    def __init__(self, kinds=("face", "plate")):
        self._kinds = set(kinds)

    def kinds(self):
        return set(self._kinds)

    def detect(self, image, kinds, threshold=redaction.THRESHOLD):
        return synthetic_detections(image, set(kinds) & self._kinds)


def box(x, y, width, height):
    return {"x": x / FRAME_WIDTH, "y": y / FRAME_HEIGHT, "width": width / FRAME_WIDTH, "height": height / FRAME_HEIGHT}


class PureTests(unittest.TestCase):
    def test_an_ssd_model_reads_rows_keeps_its_own_label_and_drops_weak_ones(self):
        rows = np.array([[[[0, 1, 0.9, 0.1, 0.1, 0.3, 0.3], [0, 2, 0.8, 0.5, 0.5, 0.9, 0.7], [0, 2, 0.2, 0.0, 0.0, 0.1, 0.1], [0, 2, 0.9, 0.6, 0.6, 0.6, 0.9], [-1, 0, 0, 0, 0, 0, 0]]]], dtype=np.float32)
        seen = []
        plates = SsdModel(lambda tensor: seen.append(tensor.shape) or {"out": rows}, (300, 300), "plate", label=2)
        found = plates.detect(np.zeros((720, 1280, 3), dtype=np.uint8), 0.4)
        self.assertEqual(seen, [(1, 3, 300, 300)])
        self.assertEqual([item["kind"] for item in found], ["plate"])
        self.assertAlmostEqual(found[0]["box"]["width"], 0.4, places=5)
        faces = SsdModel(lambda tensor: rows, (256, 256), "face")
        self.assertEqual(len(faces.detect(np.zeros((720, 1280, 3), dtype=np.uint8), 0.4)), 2)  # Every label counts for a face model

    def test_a_detector_answers_only_for_the_kinds_it_has(self):
        model = SsdModel(lambda tensor: np.zeros((1, 1, 1, 7), dtype=np.float32), (8, 8), "face")
        detector = RedactionDetector({"face": model}, "x")
        self.assertEqual(detector.kinds(), {"face"})
        self.assertEqual(detector.detect(np.zeros((720, 1280, 3), dtype=np.uint8), ["face", "plate"]), [])

    def test_loading_needs_the_models_files_and_a_kind_without_them_is_not_offered(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            self.assertIsNone(redaction.load_detector(path))
            for name in ("face.xml", "face.bin"):
                (path / name).write_bytes(b"x")

            class Compiled:
                def input(self, _):
                    return mock.Mock(shape=[1, 3, 256, 256])

                def __call__(self, tensor):
                    return {}

            core = mock.Mock()
            core.compile_model.return_value = Compiled()
            detector = redaction.load_detector(path, core=core)
            self.assertEqual(detector.kinds(), {"face"})
            self.assertEqual(detector.models["face"].size, (256, 256))
            core.compile_model.side_effect = RuntimeError("broken model")
            self.assertIsNone(redaction.load_detector(path, core=core))

    def test_detections_link_into_one_track_per_object_and_far_apart_ones_do_not(self):
        def at(second, *boxes, kind="face"):
            return second, [{"kind": kind, "confidence": 0.9, "box": item} for item in boxes]
        frames = [at(0.0, box(100, 100, 80, 80), box(900, 300, 80, 80)), at(0.2, box(104, 100, 80, 80), box(900, 300, 80, 80)), at(0.4, box(108, 100, 80, 80)),
                  at(3.0, box(110, 100, 80, 80))]
        tracks = redaction.link(frames, "cam")
        self.assertEqual(sorted((track["start"], track["end"], len(track["samples"])) for track in tracks), [(0.0, 0.2, 2), (0.0, 0.4, 3), (3.0, 3.0, 1)])
        self.assertTrue(all(track["blur"] and track["camera_id"] == "cam" and track["id"].startswith("t-") for track in tracks))
        plates = redaction.link([at(0.0, box(100, 100, 80, 80), kind="plate")], "cam")
        self.assertEqual(plates[0]["kind"], "plate")

    def test_a_box_between_sightings_is_interpolated_and_covers_its_subject_a_little_beyond_them(self):
        samples = [[1.0, 0.1, 0.2, 0.1, 0.1], [2.0, 0.3, 0.4, 0.1, 0.1]]
        self.assertEqual(redaction._interpolate(samples, 1.5, redaction.HOLD), (0.2, 0.30000000000000004, 0.1, 0.1))
        self.assertEqual(redaction._interpolate(samples, 0.7, redaction.HOLD), (0.1, 0.2, 0.1, 0.1))
        self.assertEqual(redaction._interpolate(samples, 2.3, redaction.HOLD), (0.3, 0.4, 0.1, 0.1))
        self.assertIsNone(redaction._interpolate(samples, 0.5, redaction.HOLD))
        self.assertIsNone(redaction._interpolate(samples, 2.5, redaction.HOLD))

    def test_manual_regions_follow_a_track_or_move_between_keyframes(self):
        track = {"id": "t-a", "camera_id": "cam", "kind": "face", "blur": False, "samples": [[0.0, 0.1, 0.1, 0.1, 0.1], [2.0, 0.5, 0.1, 0.1, 0.1]]}
        followed = {"id": "m-1", "camera_id": "cam", "start": 0.0, "end": 2.0, "follow_track_id": "t-a", "scale": 2.0, "keyframes": []}
        free = {"id": "m-2", "camera_id": "cam", "start": 0.0, "end": 2.0, "follow_track_id": None, "scale": 1.0,
                "keyframes": [{"t": 0.0, "x": 0.0, "y": 0.5, "width": 0.1, "height": 0.1}, {"t": 2.0, "x": 0.8, "y": 0.5, "width": 0.1, "height": 0.1}]}
        regions = {"tracks": [track], "manual": [followed, free]}
        halfway = redaction.boxes_at(regions, "cam", 1.0)  # The track itself is kept: only the two regions are blurred
        self.assertEqual(len(halfway), 2)
        self.assertAlmostEqual(halfway[0][0] + halfway[0][2] / 2, 0.35, places=5)  # Centred on the track, twice its size
        self.assertAlmostEqual(halfway[0][2], 0.2, places=5)
        self.assertAlmostEqual(halfway[1][0], 0.4, places=5)
        self.assertEqual(redaction.boxes_at(regions, "other", 1.0), [])
        self.assertEqual(redaction.boxes_at(regions, "cam", 2.5), [])
        track["blur"] = True
        self.assertEqual(len(redaction.boxes_at(regions, "cam", 1.0)), 3)

    def test_blurring_a_frame_changes_only_the_boxes_and_leaves_nothing_readable(self):
        rng = np.random.default_rng(1)
        image = rng.integers(0, 256, (FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
        before = image.copy()
        self.assertEqual(redaction.redact_frame(image, [tuple(value / size for value, size in zip(FACE_A, (FRAME_WIDTH, FRAME_HEIGHT, FRAME_WIDTH, FRAME_HEIGHT)))]), 1)
        self.assertTrue(np.array_equal(image[400:, :], before[400:, :]))
        self.assertTrue(np.array_equal(image[:, 400:], before[:, 400:]))
        inside = image[100:180, 100:180].astype(float)
        self.assertLess(inside.std(), 25)  # Noise of 74 becomes a handful of flat blocks
        self.assertLessEqual(len({tuple(pixel) for pixel in image[100:180, 100:180].reshape(-1, 3)}), 40)
        self.assertGreater(before[100:180, 100:180].std(), 70)

    def test_a_box_at_the_edge_is_clipped_and_an_empty_one_blurs_nothing(self):
        image = np.full((FRAME_HEIGHT, FRAME_WIDTH, 3), 90, dtype=np.uint8)
        self.assertEqual(redaction.redact_frame(image, [(0.99, 0.99, 0.2, 0.2), (0.5, 0.5, 0.0, 0.0)]), 2)


@unittest.skipUnless(FFMPEG, "FFmpeg is required to render and decode the exports")
class RedactionCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_STORAGE_VOLUMES")}
        self.root = Path(self.temporary.name) / "recordings"
        self.root.mkdir()
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root)
        os.environ.pop("NVR_STORAGE_VOLUMES", None)
        initialize()
        self.camera = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'reviewer','unused','operator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(2,'other','unused','operator')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (self.camera, "Front door"))
        self.media = self.record(self.camera, faces=True)
        self.user = 1
        app.dependency_overrides[current_user] = lambda: principal(self.user, "reviewer" if self.user == 1 else "other", "operator")
        self.detector = mock.patch.object(redaction, "shared_detector", lambda: StandIn())
        self.detector.start()
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.detector.stop()
        app.dependency_overrides.pop(current_user, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def record(self, camera: str, faces: bool, name: str = "clip.mp4", seconds: int = 3, start: str = "2026-10-20T10:00:00+00:00") -> Path:
        """Three seconds of noise, with two red squares (faces) and a yellow rectangle (a plate) when asked."""
        folder = self.root / camera
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        boxes = "".join(f",drawbox=x={x}:y={y}:w={w}:h={h}:color={colour}:t=fill" for (x, y, w, h), colour in ((FACE_A, "red"), (FACE_B, "red"), (PLATE, "yellow"))) if faces else ""
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=gray:s=1280x720:r=10,noise=alls=60:allf=t{boxes}",
                        "-t", str(seconds), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(path)], check=True, timeout=60)
        with connect() as db:
            end = (datetime.fromisoformat(start) + timedelta(seconds=seconds)).isoformat()
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)", (camera, f"{camera}/{name}", start, end, path.stat().st_size))
        return path

    # --- the flow ---

    def review(self, **payload) -> dict:
        body = {"camera_ids": [self.camera], "start": START, "end": END, **payload}
        response = self.client.post("/api/redaction/reviews", json=body)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def analysed(self, review: dict | None = None) -> dict:
        review = review or self.review()
        asyncio.run(redaction.advance_reviews())
        result = self.client.get(f"/api/redaction/reviews/{review['id']}")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["state"], "ready", result.json())
        return result.json()

    def export(self, review: str, **payload) -> dict:
        response = self.client.post("/api/exports", json={"camera_ids": [self.camera], "start": START, "end": END, "output_modes": ["individual"], "redaction_review_id": review, **payload})
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait(response.json()["id"])

    def wait(self, job_id: str, *terminal: str) -> dict:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            result = self.client.get(f"/api/exports/{job_id}").json()
            if result["status"] in (terminal or ("completed", "failed", "cancelled")):
                return result
            time.sleep(0.1)
        self.fail(f"Export {job_id} did not finish: {result}")

    def package(self, job: dict) -> zipfile.ZipFile:
        self.assertEqual(job["status"], "completed", job)
        download = self.client.get(job["download_url"])
        self.assertEqual(download.status_code, 200)
        self.content = download.content
        return zipfile.ZipFile(io.BytesIO(download.content))

    def frame(self, archive: zipfile.ZipFile, name: str, at: float) -> np.ndarray:
        path = Path(self.temporary.name) / "decoded.mp4"
        path.write_bytes(archive.read(name))
        data = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray",
                               "-s", f"{FRAME_WIDTH}x{FRAME_HEIGHT}", "-"], capture_output=True, check=True, timeout=30).stdout
        return np.frombuffer(data, dtype=np.uint8).reshape(FRAME_HEIGHT, FRAME_WIDTH)

    @staticmethod
    def spread(frame: np.ndarray, area: tuple[int, int, int, int], margin: int = 12) -> float:
        """How much texture remains in the ring just outside a fixture shape (inside the part a blur covers)."""
        x, y, width, height = area
        return float(frame[y + 8:y + height - 8, x - margin + 2:x - 2].std())

    def track_at(self, review: dict, area: tuple[int, int, int, int]) -> dict:
        centre = (area[0] + area[2] / 2) / FRAME_WIDTH, (area[1] + area[3] / 2) / FRAME_HEIGHT
        return next(track for track in review["tracks"] if abs(track["box"]["x"] + track["box"]["width"] / 2 - centre[0]) < 0.02 and abs(track["box"]["y"] + track["box"]["height"] / 2 - centre[1]) < 0.02)


class ReviewTests(RedactionCase):
    def test_a_review_finds_the_faces_and_the_plate_and_lists_them_as_tracks(self):
        review = self.analysed()
        self.assertEqual(sorted(track["kind"] for track in review["tracks"]), ["face", "face", "plate"])
        self.assertTrue(all(track["blur"] and 0 <= track["start"] <= track["end"] <= 3.0 for track in review["tracks"]))
        self.assertEqual(review["durations"], {self.camera: 3.0})
        self.assertEqual(review["kinds"], ["faces", "plates"])
        self.assertIsNone(review["confirmed_at"])
        self.assertNotIn("samples", review["tracks"][0])

    def test_only_the_kinds_asked_for_are_looked_for(self):
        review = self.analysed(self.review(kinds=["plates"]))
        self.assertEqual([track["kind"] for track in review["tracks"]], ["plate"])

    def test_a_review_names_what_models_can_do_and_refuses_what_they_cannot(self):
        self.assertEqual(self.client.get("/api/redaction/models").json(), {"kinds": ["faces", "plates"]})
        with mock.patch.object(redaction, "shared_detector", lambda: StandIn(("face",))):
            self.assertEqual(self.client.get("/api/redaction/models").json(), {"kinds": ["faces"]})
            refused = self.client.post("/api/redaction/reviews", json={"camera_ids": [self.camera], "start": START, "end": END, "kinds": ["plates"]})
            self.assertEqual(refused.status_code, 422)
            self.assertIn("plates", refused.json()["detail"])
        with mock.patch.object(redaction, "shared_detector", lambda: None):
            self.assertEqual(self.client.get("/api/redaction/models").json(), {"kinds": []})
            self.assertEqual(self.client.post("/api/redaction/reviews", json={"camera_ids": [self.camera], "start": START, "end": END}).status_code, 422)
        self.assertEqual(self.client.post("/api/redaction/reviews", json={"camera_ids": [self.camera], "start": START, "end": END, "kinds": []}).status_code, 422)

    def test_a_review_is_bounded_and_needs_footage_and_a_known_camera(self):
        post = lambda **changes: self.client.post("/api/redaction/reviews", json={"camera_ids": [self.camera], "start": START, "end": END, **changes})  # noqa: E731
        self.assertEqual(post(end="2026-10-20T13:00:01Z").status_code, 422)  # More than two hours of footage
        self.assertEqual(post(start="2026-10-21T10:00:00Z", end="2026-10-21T10:00:03Z").status_code, 422)  # No footage then
        self.assertEqual(post(camera_ids=[str(uuid.uuid4())]).status_code, 422)
        self.assertEqual(post(start=END, end=START).status_code, 422)
        self.assertEqual(post(start="2026-10-20T10:00:00", end="2026-10-20T10:00:03").status_code, 422)
        self.assertEqual(self.post_status(post(), 202), 202)
        self.assertEqual(self.post_status(post(), 202), 202)
        self.assertEqual(post().status_code, 429)  # Two are waiting for the analysis already

    def post_status(self, response, expected):
        self.assertEqual(response.status_code, expected, response.text)
        return response.status_code

    def test_the_picture_for_review_shows_the_footage_with_what_will_be_blurred_outlined(self):
        review = self.analysed()
        face = self.track_at(review, FACE_A)
        keep = self.client.patch(f"/api/redaction/reviews/{review['id']}", json={"keep": [self.track_at(review, FACE_B)["id"]]})
        self.assertEqual(keep.status_code, 200, keep.text)
        response = self.client.get(f"/api/redaction/reviews/{review['id']}/frame", params={"camera_id": self.camera, "t": 1.0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.content[:2], b"\xff\xd8")
        self.assertEqual(face["kind"], "face")
        with connect() as db:
            events = db.execute("SELECT kind,reference FROM viewing_audit WHERE reference=?", (review["id"],)).fetchall()
        self.assertEqual({row["kind"] for row in events}, {"export"})
        self.assertEqual(self.client.get(f"/api/redaction/reviews/{review['id']}/frame", params={"camera_id": str(uuid.uuid4()), "t": 1.0}).status_code, 404)
        self.assertEqual(self.client.get(f"/api/redaction/reviews/{review['id']}/frame", params={"camera_id": self.camera, "t": 50}).status_code, 404)

    def test_the_picture_waits_for_the_analysis(self):
        review = self.review()
        self.assertEqual(self.client.get(f"/api/redaction/reviews/{review['id']}/frame", params={"camera_id": self.camera, "t": 1.0}).status_code, 409)

    def test_a_manual_region_is_checked_before_anything_changes(self):
        review = self.analysed()
        url = f"/api/redaction/reviews/{review['id']}"
        keyframe = {"t": 0.5, "x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}
        good = {"camera_id": self.camera, "start": 0.0, "end": 2.0, "keyframes": [keyframe]}
        for bad in ({**good, "camera_id": str(uuid.uuid4())}, {**good, "end": 99.0}, {**good, "start": 2.0, "end": 1.0}, {**good, "keyframes": []},
                    {**good, "keyframes": [{**keyframe, "x": 0.95}]}, {**good, "keyframes": [{**keyframe, "width": 0}]},
                    {**good, "keyframes": [], "follow_track_id": "t-nothing"},
                    {**good, "keyframes": [], "follow_track_id": review["tracks"][0]["id"], "scale": 10}):
            response = self.client.patch(url, json={"keep": [review["tracks"][0]["id"]], "manual": [bad]})
            self.assertEqual(response.status_code, 422, (bad, response.text))
        self.assertTrue(all(track["blur"] for track in self.client.get(url).json()["tracks"]))  # The valid half of a refused edit was not kept
        self.assertEqual(self.client.patch(url, json={"keep": ["t-unknown"]}).status_code, 422)
        self.assertEqual(self.client.patch(url, json={"manual": [good] * 51}).status_code, 422)
        edited = self.client.patch(url, json={"manual": [good, {"camera_id": self.camera, "start": 0, "end": 1, "follow_track_id": review["tracks"][0]["id"], "scale": 1.5}]}).json()
        self.assertEqual([region["id"] for region in edited["manual"]], ["m-1", "m-2"])
        self.assertEqual(edited["manual"][1]["scale"], 1.5)
        self.assertIsNotNone(edited["edited_at"])

    def test_a_confirmed_review_cannot_be_changed_and_an_unfinished_one_cannot_be_confirmed(self):
        pending = self.review()
        self.assertEqual(self.client.post(f"/api/redaction/reviews/{pending['id']}/confirm").status_code, 409)
        self.assertEqual(self.client.patch(f"/api/redaction/reviews/{pending['id']}", json={"keep": []}).status_code, 409)
        review = self.analysed(pending)
        confirmed = self.client.post(f"/api/redaction/reviews/{review['id']}/confirm").json()
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertIsNotNone(confirmed["confirmed_at"])
        self.assertEqual(self.client.patch(f"/api/redaction/reviews/{review['id']}", json={"keep": []}).status_code, 409)
        self.assertEqual(self.client.post(f"/api/redaction/reviews/{review['id']}/confirm").status_code, 409)

    def test_a_review_belongs_to_the_person_who_made_it_and_an_administrator_sees_all(self):
        review = self.analysed()
        self.user = 2
        for call in (lambda: self.client.get(f"/api/redaction/reviews/{review['id']}"), lambda: self.client.post(f"/api/redaction/reviews/{review['id']}/confirm"),
                     lambda: self.client.patch(f"/api/redaction/reviews/{review['id']}", json={"keep": []}),
                     lambda: self.client.get(f"/api/redaction/reviews/{review['id']}/frame", params={"camera_id": self.camera, "t": 1})):
            self.assertEqual(call().status_code, 404)
        self.assertEqual(self.client.get(f"/api/redaction/reviews/{uuid.uuid4()}").json(), self.client.get(f"/api/redaction/reviews/{review['id']}").json())
        app.dependency_overrides[current_user] = lambda: principal(3, "boss", "administrator")
        self.assertEqual(self.client.get(f"/api/redaction/reviews/{review['id']}").status_code, 200)

    def test_asking_is_audited_and_records_who_looked_at_which_cameras(self):
        review = self.review()
        with connect() as db:
            actions = [row[0] for row in db.execute("SELECT action FROM audit_log WHERE resource_id=?", (review["id"],))]
            viewed = db.execute("SELECT username,kind FROM viewing_audit WHERE reference=?", (review["id"],)).fetchall()
        self.assertEqual(actions, ["redaction_review_requested"])
        self.assertEqual([(row["username"], row["kind"]) for row in viewed], [("reviewer", "export")])

    def test_a_review_the_application_stopped_in_the_middle_of_is_failed_and_old_unused_ones_are_forgotten(self):
        stopped, used, old = self.review(), self.analysed(), self.analysed(self.review())
        with connect() as db:
            db.execute("UPDATE redaction_reviews SET state='analysing' WHERE id=?", (stopped["id"],))
            db.execute("UPDATE redaction_reviews SET state='used' WHERE id=?", (used["id"],))
        redaction.recover()
        failed = self.client.get(f"/api/redaction/reviews/{stopped['id']}").json()
        self.assertEqual((failed["state"], failed["error"]), ("failed", "Interrupted by application restart"))
        redaction.expire_reviews(datetime.now(timezone.utc) + timedelta(days=2))
        self.assertEqual(self.client.get(f"/api/redaction/reviews/{used['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/redaction/reviews/{old['id']}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/redaction/reviews/{stopped['id']}").status_code, 404)

    def test_an_analysis_that_cannot_read_the_footage_fails_with_a_reason(self):
        review = self.review()
        self.media.unlink()
        asyncio.run(redaction.advance_reviews())
        failed = self.client.get(f"/api/redaction/reviews/{review['id']}").json()
        self.assertEqual(failed["state"], "failed")
        self.assertIn("unavailable", failed["error"])
        self.assertFalse((self.root / ".redaction" / review["id"]).exists())
        from app.retention import playback_is_leased
        self.assertFalse(playback_is_leased(self.media))


class ExportTests(RedactionCase):
    def confirmed(self, keep=(), manual=None, **review) -> dict:
        analysed = self.analysed(self.review(**review))
        edit = {}
        if keep:
            edit["keep"] = [self.track_at(analysed, area)["id"] for area in keep]
        if manual is not None:
            edit["manual"] = manual
        if edit:
            self.assertEqual(self.client.patch(f"/api/redaction/reviews/{analysed['id']}", json=edit).status_code, 200)
        return self.client.post(f"/api/redaction/reviews/{analysed['id']}/confirm").json()

    def test_faces_and_plates_are_blurred_in_the_decoded_frames_and_the_kept_subject_is_not(self):
        before = hashlib.sha256(self.media.read_bytes()).hexdigest()
        review = self.confirmed(keep=[FACE_B])
        job = self.export(review["id"])
        archive = self.package(job)
        name = next(item for item in archive.namelist() if item.startswith("camera-"))
        for at in (0.5, 1.5, 2.5):
            frame = self.frame(archive, name, at)
            self.assertLess(self.spread(frame, FACE_A), 12, f"face A at {at}")
            self.assertLess(self.spread(frame, PLATE), 12, f"plate at {at}")
            self.assertGreater(self.spread(frame, FACE_B), 25, f"the kept subject at {at}")
            self.assertGreater(float(frame[500:560, 300:360].std()), 25)  # Elsewhere in the picture nothing is touched
        self.assertEqual(hashlib.sha256(self.media.read_bytes()).hexdigest(), before, "the recording itself is never changed")

    def test_the_package_holds_only_the_redacted_clip_and_its_manifest_records_the_review(self):
        review = self.confirmed(keep=[FACE_B])
        archive = self.package(self.export(review["id"]))
        names = archive.namelist()
        self.assertEqual(sorted(names), sorted(["metadata.json", "chain.json", "integrity.json", next(item for item in names if item.startswith("camera-"))]))
        self.assertFalse([item for item in names if "unredacted" in item or item == "grid.mp4"])
        for leftover in (self.root / "exports").iterdir():
            self.assertFalse(leftover.is_dir(), "no work folder, and so no unblurred render, is left behind")
        metadata = json.loads(archive.read("metadata.json"))
        record = metadata["redaction"]
        self.assertEqual((record["applied"], record["review_id"], record["method"], record["unredacted_copy_included"], record["original_footage_unchanged"]), (True, review["id"], "pixelate", False, True))
        self.assertEqual(record["reviewer"]["username"], "reviewer")
        self.assertEqual(record["reviewer"]["user_id"], 1)
        self.assertEqual(record["kinds"], ["faces", "plates"])
        self.assertEqual(record["kept_track_ids"], [self.track_at(self.client.get(f"/api/redaction/reviews/{review['id']}").json(), FACE_B)["id"]])
        self.assertEqual(record["settings"]["model"], "redaction:test")
        self.assertEqual((record["settings"]["sample_fps"], record["settings"]["threshold"]), (redaction.SAMPLE_FPS, redaction.THRESHOLD))
        camera = metadata["cameras"][0]
        self.assertEqual((camera["redaction"]["tracks_blurred"], camera["redaction"]["tracks_kept"], camera["redaction"]["manual_regions"]), (2, 1, 0))
        self.assertGreater(camera["redaction"]["blurred_boxes"], 40)
        self.assertEqual({item["path"] for item in json.loads(archive.read("integrity.json"))["files"]}, {"metadata.json", "chain.json", camera["video_file"]})

    def test_the_package_still_verifies_and_a_changed_clip_does_not(self):
        job = self.export(self.confirmed()["id"])
        archive = self.package(job)
        verified = self.client.post(f"/api/exports/{job['id']}/verify", content=self.content)
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertTrue(verified.json()["checksums_valid"])
        self.assertTrue(verified.json()["chain"]["valid"])
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as changed:
            for item in archive.namelist():
                changed.writestr(item, archive.read(item) + (b"x" if item.startswith("camera-") else b""))
        self.assertFalse(self.client.post(f"/api/exports/{job['id']}/verify", content=buffer.getvalue()).json()["checksums_valid"])

    def test_a_manual_region_follows_its_keyframes_across_frames(self):
        self.record(self.camera, faces=False, name="plain.mp4", start="2026-10-20T10:01:00+00:00")
        start, end = "2026-10-20T10:01:00Z", "2026-10-20T10:01:03Z"
        review = self.review(start=start, end=end)
        asyncio.run(redaction.advance_reviews())
        moving = {"camera_id": self.camera, "start": 0.0, "end": 3.0, "keyframes": [
            {"t": 0.0, "x": 100 / FRAME_WIDTH, "y": 300 / FRAME_HEIGHT, "width": 80 / FRAME_WIDTH, "height": 80 / FRAME_HEIGHT},
            {"t": 3.0, "x": 1000 / FRAME_WIDTH, "y": 300 / FRAME_HEIGHT, "width": 80 / FRAME_WIDTH, "height": 80 / FRAME_HEIGHT}]}
        self.assertEqual(self.client.patch(f"/api/redaction/reviews/{review['id']}", json={"manual": [moving]}).status_code, 200)
        self.client.post(f"/api/redaction/reviews/{review['id']}/confirm")
        response = self.client.post("/api/exports", json={"camera_ids": [self.camera], "start": start, "end": end, "output_modes": ["individual"], "redaction_review_id": review["id"]})
        archive = self.package(self.wait(response.json()["id"]))
        name = next(item for item in archive.namelist() if item.startswith("camera-"))
        # The region moves 300 pixels a second: at one second it is at x=400, at two at x=700.
        for at in (1.0, 2.0):
            frame = self.frame(archive, name, at)
            here = (100 + int(300 * at), 300, 80, 80)
            self.assertLess(self.spread(frame, here), 12, f"the region at {at}")
            self.assertGreater(self.spread(frame, (100, 300, 80, 80)), 25, f"where it started, at {at}")
            self.assertGreater(self.spread(frame, (1000, 300, 80, 80)), 25, f"where it ends, at {at}")
            self.assertGreater(self.spread(frame, (here[0], 500, 80, 80)), 25, f"below it, at {at}")

    def test_a_region_that_follows_a_track_stays_with_it_even_for_a_track_that_is_kept(self):
        analysed = self.analysed()
        subject = self.track_at(analysed, FACE_B)
        below = {"camera_id": self.camera, "start": 0.0, "end": 3.0, "follow_track_id": self.track_at(analysed, FACE_A)["id"], "scale": 1.0}
        edit = self.client.patch(f"/api/redaction/reviews/{analysed['id']}", json={"keep": [subject["id"], self.track_at(analysed, FACE_A)["id"]], "manual": [below]})
        self.assertEqual(edit.status_code, 200, edit.text)
        self.client.post(f"/api/redaction/reviews/{analysed['id']}/confirm")
        archive = self.package(self.export(analysed["id"]))
        name = next(item for item in archive.namelist() if item.startswith("camera-"))
        frame = self.frame(archive, name, 1.0)
        self.assertLess(self.spread(frame, FACE_A), 12)  # Kept as a track, but the region following it blurs it
        self.assertGreater(self.spread(frame, FACE_B), 25)

    def test_an_export_needs_a_confirmed_review_of_the_same_cameras_and_range_and_individual_files(self):
        analysed = self.analysed()
        post = lambda review, **changes: self.client.post("/api/exports", json={"camera_ids": [self.camera], "start": START, "end": END, "output_modes": ["individual"],  # noqa: E731
                                                                              "redaction_review_id": review, **changes})
        self.assertEqual(post(analysed["id"]).status_code, 422)  # Not confirmed
        self.client.post(f"/api/redaction/reviews/{analysed['id']}/confirm")
        self.assertEqual(post(analysed["id"], output_modes=["individual", "grid"]).status_code, 422)
        self.assertEqual(post(analysed["id"], output_modes=["grid"]).status_code, 422)
        self.assertEqual(post(analysed["id"], end="2026-10-20T10:00:02Z").status_code, 422)
        self.assertEqual(post(str(uuid.uuid4())).status_code, 422)
        other = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (other, "Other"))
        self.record(other, faces=False)
        self.assertEqual(post(analysed["id"], camera_ids=[self.camera, other]).status_code, 422)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM exports").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM redaction_reviews WHERE id=?", (analysed["id"],)).fetchone()[0], "confirmed")
        self.assertEqual(post(analysed["id"]).status_code, 202)

    def test_a_review_makes_one_export_and_another_person_cannot_use_it(self):
        review = self.confirmed()
        self.user = 2
        stolen = self.client.post("/api/exports", json={"camera_ids": [self.camera], "start": START, "end": END, "output_modes": ["individual"], "redaction_review_id": review["id"]})
        self.assertEqual(stolen.status_code, 422)
        self.user = 1
        job = self.export(review["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["redaction_review_id"], review["id"])
        again = self.client.post("/api/exports", json={"camera_ids": [self.camera], "start": START, "end": END, "output_modes": ["individual"], "redaction_review_id": review["id"]})
        self.assertEqual(again.status_code, 422)
        with connect() as db:
            used = db.execute("SELECT state,export_id FROM redaction_reviews WHERE id=?", (review["id"],)).fetchone()
        self.assertEqual((used["state"], used["export_id"]), ("used", job["id"]))

    def test_an_export_that_cannot_be_redacted_fails_and_leaves_no_package_and_a_retry_uses_the_same_review(self):
        review = self.confirmed()
        with mock.patch.object(redaction, "redact_clip", side_effect=RuntimeError("The clip could not be redacted")):
            failed = self.export(review["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("redacted", failed["error"])
        self.assertIsNone(failed["download_url"])
        self.assertEqual(self.client.get(f"/api/exports/{failed['id']}/download").status_code, 409)
        self.assertEqual([item.name for item in (self.root / "exports").iterdir()], [])
        retry = self.client.post(f"/api/exports/{failed['id']}/retry")
        self.assertEqual(retry.status_code, 202, retry.text)
        retried = self.wait(retry.json()["id"])
        self.assertEqual(retried["status"], "completed", retried)
        self.assertEqual(retried["redaction_review_id"], review["id"])
        names = self.package(retried).namelist()
        self.assertFalse([item for item in names if "unredacted" in item])

    def test_cancelling_while_blurring_leaves_nothing_behind(self):
        review = self.confirmed()
        started = []

        def slow(source, target, regions, camera_id, cancelled):
            started.append(source.name)
            while not cancelled():
                time.sleep(0.02)
            raise redaction.Cancelled()
        with mock.patch.object(redaction, "redact_clip", slow):
            response = self.client.post("/api/exports", json={"camera_ids": [self.camera], "start": START, "end": END, "output_modes": ["individual"], "redaction_review_id": review["id"]})
            job_id = response.json()["id"]
            deadline = time.monotonic() + 30
            while not started and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(started and started[0].startswith("unredacted-"), True)
            self.assertEqual(self.client.post(f"/api/exports/{job_id}/cancel").status_code, 200)
            self.assertEqual(self.wait(job_id)["status"], "cancelled")
        self.assertEqual([item.name for item in (self.root / "exports").iterdir()], [])

    def test_an_export_without_redaction_is_as_before(self):
        response = self.client.post("/api/exports", json={"camera_ids": [self.camera], "start": START, "end": END})
        job = self.wait(response.json()["id"])
        archive = self.package(job)
        self.assertNotIn("redaction", json.loads(archive.read("metadata.json")))
        self.assertIn("grid.mp4", archive.namelist())
        self.assertIsNone(job["redaction_review_id"])


class AccessTests(GrantCase):
    """A review follows the cameras a person may see exactly as an export does."""

    def setUp(self):
        super().setUp()
        for camera in (self.d1, self.y1):
            self.add_segment(camera)
        self.detector = mock.patch.object(redaction, "shared_detector", lambda: StandIn())
        self.detector.start()

    def tearDown(self):
        self.detector.stop()
        super().tearDown()

    def ask(self, client, cameras):
        window = {"start": (START_TIME + timedelta(seconds=5)).isoformat(), "end": (START_TIME + timedelta(seconds=15)).isoformat()}
        return client.post("/api/redaction/reviews", json={"camera_ids": cameras, **window})

    def test_a_hidden_camera_is_answered_exactly_like_one_that_does_not_exist(self):
        hidden, unknown = self.ask(self.limited, [self.y1]), self.ask(self.limited, [str(uuid.uuid4())])
        self.assertEqual((hidden.status_code, hidden.json()), (unknown.status_code, unknown.json()))
        self.assertEqual(hidden.status_code, 422)
        self.assertEqual(self.ask(self.limited, [self.d1, self.y1]).status_code, 422)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM redaction_reviews").fetchone()[0], 0)
        self.assertEqual(self.ask(self.limited, [self.d1]).status_code, 202)
        self.assertEqual(self.ask(self.full, [self.y1]).status_code, 202)

    def test_a_review_of_a_camera_a_person_can_no_longer_see_is_missing_to_them(self):
        review = self.ask(self.limited, [self.d1]).json()["id"]
        self.assertEqual(self.limited.get(f"/api/redaction/reviews/{review}").status_code, 200)
        self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.yard}])
        for call in (lambda: self.limited.get(f"/api/redaction/reviews/{review}"), lambda: self.limited.post(f"/api/redaction/reviews/{review}/confirm")):
            response = call()
            self.assertEqual((response.status_code, response.json()), (404, {"detail": "Redaction review not found"}))
        self.assertEqual(self.limited.get(f"/api/redaction/reviews/{review}/frame", params={"camera_id": self.d1, "t": 1}).status_code, 404)
        self.assertEqual(self.admin.get(f"/api/redaction/reviews/{review}").status_code, 200)
