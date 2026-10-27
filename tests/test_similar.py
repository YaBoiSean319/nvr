"""Find similar (#126): appearance embeddings, the extension and the scan giving one answer, access, retention, and the worker storing them."""
from __future__ import annotations

import sqlite3
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from app import similar, tracks
from app.store import connect
from tests.scene_case import SceneTestCase
from tests.test_attribute_search import DAY, NOW, SearchCase, box_of, frame_with

PERSON_BOX = box_of(100, 40, 124, 104)

# BGR bands from head to feet for a few people
RED_BLUE = [(140, 160, 205), (30, 30, 220), (220, 60, 30), (25, 25, 25)]
GREEN_BLACK = [(150, 170, 215), (30, 200, 30), (15, 15, 15), (20, 20, 20)]
YELLOW_GRAY = [(130, 150, 200), (20, 220, 230), (120, 120, 120), (30, 30, 30)]
RED_BLACK = [(140, 160, 205), (30, 30, 220), (15, 15, 15), (25, 25, 25)]  # The nearest look-alike: the same top, different legs


def person_frame(bands: list[tuple[int, int, int]], light: float = 1.0, box: tuple[int, int, int, int] = (100, 40, 124, 104)) -> bytes:
    x1, y1, x2, y2 = box
    step = (y2 - y1) // 4
    regions = [((x1, y1 + index * step, x2, y1 + (index + 1) * step if index < 3 else y2), tuple(int(min(255, channel * light)) for channel in colour))
               for index, colour in enumerate(bands)]
    return frame_with(regions)


def vector_of(bands: list[tuple[int, int, int]], light: float = 1.0, box=(100, 40, 124, 104)) -> np.ndarray:
    return similar.colour_embedding(person_frame(bands, light, box), box_of(*box))


class EmbeddingTests(unittest.TestCase):
    def test_the_same_clothes_are_more_alike_than_different_ones(self):
        same = similar.cosine(similar.quantize(vector_of(RED_BLUE)), similar.quantize(vector_of(RED_BLUE, light=0.85, box=(60, 30, 90, 110))))
        look_alike = similar.cosine(similar.quantize(vector_of(RED_BLUE)), similar.quantize(vector_of(RED_BLACK)))
        different = similar.cosine(similar.quantize(vector_of(RED_BLUE)), similar.quantize(vector_of(GREEN_BLACK)))
        self.assertGreater(same, 0.95)
        self.assertGreater(same, look_alike)
        self.assertGreater(look_alike, different)
        self.assertLess(different, 0.6)

    def test_a_box_with_too_few_pixels_has_no_embedding(self):
        self.assertIsNone(similar.colour_embedding(person_frame(RED_BLUE), box_of(100, 40, 101, 42)))
        self.assertIsNone(similar.colour_embedding(b"short", PERSON_BOX))
        self.assertIsNone(similar.colour_embedding(None, PERSON_BOX))

    def test_quantizing_keeps_values_in_a_byte_and_the_vector_close_to_unit_length(self):
        vector = vector_of(RED_BLUE)
        data = similar.quantize(vector)
        self.assertEqual(len(data), vector.size)
        restored = np.frombuffer(data, dtype=np.int8).astype(np.float64) / similar.SCALE
        self.assertAlmostEqual(float(np.linalg.norm(restored)), 1.0, places=1)
        self.assertAlmostEqual(similar.cosine(data, data), 1.0, places=6)
        self.assertEqual(similar.cosine(data, data[:-1]), 0.0)  # Different sizes are never compared


class FakeReidCore:
    """An OpenVINO core whose re-identification model answers a fixed 256-value vector."""

    def __init__(self) -> None:
        self.shape = None

    def read_model(self, path: str):
        return path

    def compile_model(self, model, provider):
        core = self

        class Output:
            def get_any_name(self) -> str:
                return "descriptor"

        def compiled(tensor):
            core.shape = tensor.shape
            return {Output(): np.arange(1, 257, dtype=np.float32).reshape(1, 256, 1, 1)}
        return compiled


class ModelTests(unittest.TestCase):
    def test_people_use_the_reid_model_and_vehicles_and_failures_use_the_pixels(self):
        core = FakeReidCore()
        model = similar.ReidModel(Path("/models/reid.xml"), core=core)
        frame = person_frame(RED_BLUE)
        found = similar.embed(frame, "person", PERSON_BOX, model)
        self.assertEqual((found[0], found[1].size, core.shape), (similar.REID_MODEL_ID, 256, (1, 3, 256, 128)))
        self.assertAlmostEqual(float(np.linalg.norm(found[1])), 1.0, places=5)
        self.assertEqual(similar.embed(frame, "vehicle", PERSON_BOX, model)[0], similar.COLOUR_MODEL_ID)
        self.assertEqual(similar.embed(frame, "person", PERSON_BOX, None)[0], similar.COLOUR_MODEL_ID)

        class Broken:
            def embed(self, frame, box):
                raise RuntimeError("model fault")
        self.assertEqual(similar.embed(frame, "person", PERSON_BOX, Broken())[0], similar.COLOUR_MODEL_ID)
        self.assertIsNone(similar.embed(None, "person", PERSON_BOX, None))

    def test_without_the_model_files_there_is_no_model_and_no_error(self):
        self.assertIsNone(similar.load_reid_model(Path("/nowhere/reid.xml")))


class SimilarCase(SearchCase):
    """The same person (red over blue) on Dock 1, Dock 2 and Yard 1 at different times, plus look-alikes and strangers."""

    def setUp(self):
        super().setUp()
        at = NOW - timedelta(hours=5)
        self.source = self.person(self.d1, at, RED_BLUE)
        self.same_dock2 = self.person(self.d2, at + timedelta(minutes=10), RED_BLUE, light=0.9, box=(60, 30, 90, 110))
        self.same_yard = self.person(self.y1, at + timedelta(minutes=25), RED_BLUE, light=1.1, box=(130, 50, 150, 100))
        self.look_alike = self.person(self.d2, at + timedelta(minutes=5), RED_BLACK)
        self.green = self.person(self.d1, at + timedelta(minutes=15), GREEN_BLACK)
        self.yellow = self.person(self.y1, at + timedelta(minutes=20), YELLOW_GRAY)
        self.vehicle = self.add_track(self.d1, "vehicle", at + timedelta(minutes=12), vehicle_colour="red")
        with connect() as db:
            similar.store_embedding(db, self.vehicle, similar.COLOUR_MODEL_ID, vector_of(RED_BLUE))  # Looks the same, but is not a person

    def person(self, camera_id: str, at: datetime, bands, light: float = 1.0, box=(100, 40, 124, 104), model_id: str = similar.COLOUR_MODEL_ID) -> str:
        track = self.add_track(camera_id, "person", at)
        with connect() as db:
            similar.store_embedding(db, track, model_id, vector_of(bands, light, box))
        return track

    def similar(self, track: str | None = None, client: TestClient | None = None, **params):
        return (client or self.admin).get(f"/api/ai/tracks/{track or self.source}/similar", params=params)


class FindSimilarTests(SimilarCase):
    def test_the_person_is_found_on_the_other_cameras_ranked_above_the_distractors(self):
        response = self.similar(sort="similarity", min_similarity=0.0)
        self.assertEqual(response.status_code, 200, response.text)
        ordered = [item["track_id"] for item in response.json()["results"]]
        self.assertEqual(ordered[:2], [self.same_dock2, self.same_yard] if ordered[0] == self.same_dock2 else [self.same_yard, self.same_dock2])
        self.assertEqual(set(ordered[:2]), {self.same_dock2, self.same_yard})
        self.assertLess(ordered.index(self.same_dock2), ordered.index(self.look_alike))
        self.assertLess(ordered.index(self.same_yard), ordered.index(self.look_alike))
        self.assertNotIn(self.source, ordered)  # Not itself
        self.assertNotIn(self.vehicle, ordered)  # Not another kind of object

    def test_the_default_threshold_leaves_strangers_out_and_keeps_the_nearest_look_alike_below_the_person(self):
        body = self.similar().json()
        found = {item["track_id"]: item for item in body["results"]}
        self.assertEqual(set(found), {self.same_dock2, self.same_yard, self.look_alike})  # A different top and legs are not near enough
        self.assertGreater(min(found[self.same_dock2]["similarity"], found[self.same_yard]["similarity"]), 0.9)
        self.assertLess(found[self.look_alike]["similarity"], 0.8)

    def test_results_are_in_time_order_by_default_with_rank_and_similarity(self):
        body = self.similar(min_similarity=0.0).json()
        times = [item["first_seen_at"] for item in body["results"]]
        self.assertEqual(times, sorted(times))
        self.assertEqual(sorted(item["rank"] for item in body["results"]), list(range(1, len(times) + 1)))
        self.assertEqual(body["sorted_by"], "time")
        by_rank = sorted(body["results"], key=lambda item: item["rank"])
        self.assertEqual([item["similarity"] for item in by_rank], sorted((item["similarity"] for item in by_rank), reverse=True))
        self.assertTrue({"coverage", "start", "end"} <= set(body))

    def test_the_closest_are_kept_when_there_are_more_than_the_limit(self):
        body = self.similar(min_similarity=0.0, limit=2).json()
        self.assertEqual({item["track_id"] for item in body["results"]}, {self.same_dock2, self.same_yard})

    def test_no_api_answer_carries_an_embedding(self):
        text = self.similar(min_similarity=0.0).text + self.run_search_text()
        for word in ("vector", "embedding", "model_id"):
            self.assertNotIn(word, text)

    def run_search_text(self) -> str:
        return self.admin.get("/api/ai/search", params={"start": (NOW - 2 * DAY).isoformat(), "end": NOW.isoformat()}).text

    def test_a_search_can_be_limited_to_some_cameras_and_a_range(self):
        only = self.similar(min_similarity=0.0, camera_ids=[self.d2]).json()
        self.assertEqual({item["camera_id"] for item in only["results"]}, {self.d2})
        late = self.similar(min_similarity=0.0, start=(NOW - timedelta(hours=4, minutes=42)).isoformat(), end=NOW.isoformat()).json()
        self.assertEqual({item["track_id"] for item in late["results"]}, {self.same_yard, self.yellow})

    def test_only_embeddings_from_the_same_model_are_compared(self):
        other = self.person(self.d2, NOW - timedelta(hours=4, minutes=50), RED_BLUE, model_id="some-other-model")
        self.assertNotIn(other, {item["track_id"] for item in self.similar(min_similarity=0.0).json()["results"]})

    def test_an_object_with_no_appearance_cannot_be_compared(self):
        bare = self.add_track(self.d1, "person", NOW - timedelta(hours=2))
        response = self.similar(bare)
        self.assertEqual(response.status_code, 409)
        self.assertIn("no appearance", response.json()["detail"])

    def test_bad_requests_are_refused(self):
        for params in ({"min_similarity": 1.5}, {"limit": 0}, {"limit": 101}, {"sort": "random"}, {"start": "yesterday"},
                       {"start": NOW.isoformat(), "end": (NOW - DAY).isoformat()}, {"start": (NOW - 200 * DAY).isoformat(), "end": NOW.isoformat()}):
            self.assertEqual(self.similar(**params).status_code, 422, params)
        self.assertEqual(self.similar(str(uuid.uuid4())).status_code, 404)


class AccessTests(SimilarCase):
    def test_someone_limited_to_the_dock_sees_only_dock_results_and_cannot_start_from_the_yard(self):
        body = self.similar(client=self.limited, min_similarity=0.0).json()
        self.assertEqual({item["camera_id"] for item in body["results"]}, {self.d1, self.d2})
        self.assertNotIn(self.same_yard, {item["track_id"] for item in body["results"]})
        self.assertEqual(self.similar(self.same_yard, client=self.limited).status_code, 404)
        self.assertEqual(self.similar(self.same_yard, client=self.limited).json(), self.similar(str(uuid.uuid4()), client=self.limited).json())  # No hint that it exists

    def test_signing_in_is_required(self):
        self.assertEqual(TestClient(self.app).get(f"/api/ai/tracks/{self.source}/similar").status_code, 401)


class ExtensionAndScanTests(SimilarCase):
    def test_the_extension_and_the_scan_give_the_same_tracks_in_the_same_order_with_the_same_scores(self):
        with connect() as db:
            if not similar.load_extension(db):
                self.skipTest("sqlite-vec is not installed")
            cameras = [self.d1, self.d2, self.y1]
            start, end = NOW - DAY, NOW
            for threshold in (0.0, 0.6, 0.9):
                with_extension = similar.find_similar(db, self.source, cameras, start, end, min_similarity=threshold, use_extension=True)
                scanned = similar.find_similar(db, self.source, cameras, start, end, min_similarity=threshold, use_extension=False)
                self.assertEqual([item[0] for item in with_extension], [item[0] for item in scanned], threshold)
                for left, right in zip(with_extension, scanned):
                    self.assertAlmostEqual(left[1], right[1], places=5)

    def test_without_the_extension_the_scan_answers_and_asking_for_the_extension_says_it_is_missing(self):
        with mock.patch.object(similar, "EXTENSION_ENABLED", False):
            body = self.similar(min_similarity=0.9).json()
            self.assertEqual({item["track_id"] for item in body["results"]}, {self.same_dock2, self.same_yard})
            with connect() as db:
                with self.assertRaises(RuntimeError):
                    similar.find_similar(db, self.source, [self.d1], NOW - DAY, NOW, use_extension=True)

    def test_a_connection_that_cannot_load_extensions_falls_back_to_the_scan(self):
        class NoExtensions:
            def execute(self, *args):
                raise sqlite3.OperationalError("no such function: vec_version")

            def enable_load_extension(self, flag):
                raise AttributeError("not supported")
        self.assertFalse(similar.load_extension(NoExtensions()))


class RetentionTests(SimilarCase):
    def test_deleting_footage_removes_its_embeddings(self):
        with connect() as db:
            count = lambda: db.execute("SELECT COUNT(*) FROM track_embeddings").fetchone()[0]  # noqa: E731
            before = count()
            tracks.purge_expired_detections(db, self.d1, NOW)  # No footage covers these detections, so they go, and their tracks with them
            remaining = {row[0] for row in db.execute("SELECT track_id FROM track_embeddings")}
            gone_tracks = {row[0] for row in db.execute("SELECT id FROM ai_tracks WHERE camera_id=?", (self.d1,))}
        self.assertLess(count_after := len(remaining), before)
        self.assertFalse(remaining & {self.source, self.green, self.vehicle})
        self.assertEqual(gone_tracks, set())
        self.assertIn(self.same_dock2, remaining)
        self.assertTrue(count_after > 0)

    def test_deleting_a_cameras_footage_removes_its_embeddings(self):
        with connect() as db:
            db.execute("DELETE FROM ai_tracks WHERE camera_id=?", (self.d1,))  # What DELETE /api/cameras/{id}/footage does
            remaining = {row[0] for row in db.execute("SELECT track_id FROM track_embeddings")}
        self.assertFalse(remaining & {self.source, self.green, self.vehicle})
        self.assertIn(self.same_dock2, remaining)


class WorkerStoresEmbeddingsTests(SceneTestCase):
    def test_the_worker_stores_an_embedding_for_each_track_and_search_finds_by_it(self):
        self.analyse_footage()
        window = self.scene_range()
        found = self.client.get("/api/ai/tracks", params=window).json()["tracks"]
        self.assertTrue(found)
        with connect() as db:
            stored = {row["track_id"]: row for row in db.execute("SELECT track_id,model_id,dim,length(vector) AS size FROM track_embeddings")}
        self.assertEqual(set(stored), {track["id"] for track in found})
        for row in stored.values():
            self.assertEqual((row["model_id"], row["dim"], row["size"]), (similar.COLOUR_MODEL_ID, similar.BANDS * (similar.HUE_BINS + similar.LEVEL_BINS), similar.BANDS * (similar.HUE_BINS + similar.LEVEL_BINS)))
        person = next(track for track in found if track["label"] == "person")
        response = self.client.get(f"/api/ai/tracks/{person['id']}/similar", params={"min_similarity": 0.0, "start": window["start"], "end": window["end"]})
        self.assertEqual(response.status_code, 200, response.text)
        for item in response.json()["results"]:
            self.assertEqual(item["label"], "person")


if __name__ == "__main__":
    unittest.main()
