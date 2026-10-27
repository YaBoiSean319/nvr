"""Natural-language search (#127): the tokenizer, what a description is understood to ask for, ranking, narrowing, access, retention and the worker indexing."""
from __future__ import annotations

import json
import socket
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np

from app import clip, semantic
from app.attributes import PALETTE
from app.store import connect
from tests.scene_case import SceneTestCase
from tests.test_attribute_search import DAY, NOW, SearchCase, box_of, frame_with

COLOURS = {"red": (30, 30, 220), "blue": (220, 40, 30), "black": (10, 10, 10), "white": (240, 240, 240), "gray": (120, 120, 120), "green": (30, 200, 30),
           "yellow": (20, 220, 230), "orange": (20, 140, 250), "purple": (200, 40, 150), "pink": (190, 120, 250), "brown": (30, 60, 110)}
WORDS = [*PALETTE, "grey", "van", "car", "truck", "bus", "person", "man", "jacket", "a", "the", "of"]
BOX = box_of(100, 40, 160, 100)
MIDNIGHT = NOW.replace(hour=0, minute=0, second=0)


def make_tokenizer(words: list[str]) -> clip.Tokenizer:
    """A tokenizer whose vocabulary holds each word whole, built from merges the way CLIP's own merges build its words."""
    vocab: dict[str, int] = {"<|startoftext|>": clip.START, "<|endoftext|>": clip.END}
    merges: list[tuple[str, str]] = []
    next_id = 1
    for word in words:
        pieces = list(word[:-1]) + [word[-1] + "</w>"]
        for piece in pieces:
            if piece not in vocab:
                vocab[piece] = next_id
                next_id += 1
        joined = pieces[0]
        for piece in pieces[1:]:
            merges.append((joined, piece))
            joined += piece
            if joined not in vocab:
                vocab[joined] = next_id
                next_id += 1
    return clip.Tokenizer(vocab, merges)


def fake_model(model_id: str = "clip:fixture") -> clip.ClipModel:
    """A model whose shared space has one axis for each palette colour: a picture lies along its dominant colour, a text along the colours it names."""
    tokenizer = make_tokenizer(WORDS)
    word_axis = {tokenizer.vocab[f"{word}</w>"]: PALETTE.index({"grey": "gray"}.get(word, word)) for word in WORDS if {"grey": "gray"}.get(word, word) in PALETTE}
    palette = np.array([COLOURS[name] for name in PALETTE], dtype=np.float32)

    def text(tokens: np.ndarray):
        vector = np.zeros(len(PALETTE), dtype=np.float32)
        for token in tokens.reshape(-1):
            if int(token) in word_axis:
                vector[word_axis[int(token)]] += 1.0
        return {"text": vector + 0.01}

    def image(tensor: np.ndarray):
        rgb = (tensor[0].transpose(1, 2, 0) * clip.STD + clip.MEAN).reshape(-1, 3).mean(axis=0) * 255.0
        distance = np.linalg.norm(palette[:, ::-1] - rgb, axis=1)  # The palette is BGR, the encoder's input RGB
        vector = np.exp(-distance / 40.0).astype(np.float32)
        return {"image": vector}
    return clip.ClipModel(image, text, tokenizer, model_id)


def picture(colour: str) -> bytes:
    return frame_with([((100, 40, 160, 100), COLOURS[colour])])


class TokenizerTests(unittest.TestCase):
    def test_words_become_their_ids_between_start_and_end_and_the_rest_is_padding(self):
        tokenizer = make_tokenizer(["white", "van"])
        tokens = tokenizer.encode("White   VAN!")
        self.assertEqual(tokens.shape, (clip.CONTEXT,))
        self.assertEqual(tokens[0], clip.START)
        self.assertEqual(list(tokens[1:3]), [tokenizer.vocab["white</w>"], tokenizer.vocab["van</w>"]])
        self.assertEqual(tokens[3], clip.END)
        self.assertFalse(tokens[4:].any())

    def test_a_long_description_is_cut_to_fit_and_still_ends(self):
        tokens = make_tokenizer(["van"]).encode("van " * 200)
        self.assertEqual((tokens[0], tokens[clip.CONTEXT - 1]), (clip.START, clip.END))

    def test_the_files_of_a_model_load_into_a_tokenizer(self):
        tokenizer = make_tokenizer(["van"])
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "vocab.json").write_text(json.dumps(tokenizer.vocab))
            Path(folder, "merges.txt").write_text("#version: 0.2\n" + "\n".join(" ".join(pair) for pair in tokenizer.ranks))
            loaded = clip.Tokenizer.from_files(Path(folder, "vocab.json"), Path(folder, "merges.txt"))
        self.assertEqual(list(loaded.encode("van")[:3]), list(tokenizer.encode("van")[:3]))


class ModelTests(unittest.TestCase):
    def test_a_model_with_missing_files_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(clip.load_clip_model(Path(folder)))
            Path(folder, "image_encoder.xml").write_text("x")
            self.assertIsNone(clip.load_clip_model(Path(folder)))

    def test_an_object_is_embedded_as_a_unit_vector_and_a_tiny_box_is_not(self):
        model = fake_model()
        vector = model.embed_image(picture("white"), BOX)
        self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=5)
        self.assertEqual(int(np.argmax(vector)), PALETTE.index("white"))
        self.assertIsNone(model.embed_image(picture("white"), box_of(100, 40, 101, 41)))
        self.assertIsNone(model.embed_image(None, BOX))


class ParseTests(unittest.TestCase):
    CAMERAS = {"c-gate": "Front Gate", "c-dock1": "Dock 1", "c-dock2": "Dock 2", "c-yard": "Yard Camera", "c-back": "Back Door"}
    GROUPS = {"g-docks": ("Loading", ["c-dock1", "c-dock2"])}
    NOW = datetime(2026, 10, 14, 15, 30, tzinfo=timezone.utc)

    def parse(self, text: str, offset: int = 0) -> semantic.Parsed:
        return semantic.parse(text, self.CAMERAS, self.GROUPS, now=self.NOW, utc_offset_minutes=offset)

    def test_the_description_is_what_is_left_after_time_and_camera_phrases(self):
        parsed = self.parse("white van at the gate after 6 pm")
        self.assertEqual(parsed.text, "white van")
        self.assertEqual(parsed.camera_ids, ["c-gate"])
        self.assertEqual(parsed.time_of_day, (18 * 60, 24 * 60))
        self.assertEqual((parsed.label, parsed.facets), ("vehicle", {"vehicle_type": "van", "vehicle_colour": "white"}))

    def test_clock_phrases(self):
        self.assertEqual(self.parse("man after 18:30").time_of_day, (18 * 60 + 30, 24 * 60))
        self.assertEqual(self.parse("man before 9 am").time_of_day, (0, 9 * 60))
        self.assertEqual(self.parse("man before 9:15 pm").time_of_day, (0, 21 * 60 + 15))
        self.assertEqual(self.parse("man between 6 and 8 pm").time_of_day, (18 * 60, 20 * 60))
        self.assertEqual(self.parse("man between 11 pm and 2 am").time_of_day, (23 * 60, 2 * 60))
        self.assertEqual(self.parse("man at night").time_of_day, (22 * 60, 5 * 60))
        self.assertEqual(self.parse("man in the morning").time_of_day, (5 * 60, 12 * 60))
        self.assertIsNone(self.parse("man after 99 pm").time_of_day)
        for ambiguous in ("man after 6", "van from 3 cameras", "van by 2 trees", "man before 7"):
            parsed = self.parse(ambiguous)
            self.assertIsNone(parsed.time_of_day, ambiguous)
            self.assertEqual(parsed.text, ambiguous)
        self.assertEqual(self.parse("man after 14").time_of_day, (14 * 60, 24 * 60))
        self.assertEqual(self.parse("man after 6 pm").text, "man")

    def test_range_phrases_are_in_the_viewers_local_time(self):
        yesterday = self.parse("red car yesterday")
        self.assertEqual((yesterday.start, yesterday.end), (datetime(2026, 10, 13, tzinfo=timezone.utc), datetime(2026, 10, 14, tzinfo=timezone.utc)))
        shifted = self.parse("red car yesterday", offset=-300)  # Five hours behind UTC: it is 10:30 there, still the 14th
        self.assertEqual(shifted.start, datetime(2026, 10, 13, 5, tzinfo=timezone.utc))
        self.assertEqual(self.parse("car in the last 2 hours").start, self.NOW - timedelta(hours=2))
        self.assertEqual(self.parse("car last 30 minutes").start, self.NOW - timedelta(minutes=30))
        self.assertEqual(self.parse("car past week").start, self.NOW - timedelta(weeks=1))
        today = self.parse("car today")
        self.assertEqual((today.start, today.end), (datetime(2026, 10, 14, tzinfo=timezone.utc), self.NOW))
        night = self.parse("car last night")
        self.assertEqual((night.start, night.end), (datetime(2026, 10, 13, 18, tzinfo=timezone.utc), datetime(2026, 10, 14, 6, tzinfo=timezone.utc)))
        self.assertEqual(self.parse("car this morning").end, datetime(2026, 10, 14, 12, tzinfo=timezone.utc))

    def test_cameras_are_found_by_name_group_or_a_word_that_tells_them_apart(self):
        self.assertEqual(self.parse("person at Dock 1").camera_ids, ["c-dock1"])
        self.assertEqual(self.parse("person near the front gate").camera_ids, ["c-gate"])
        self.assertEqual(self.parse("person at the gate").camera_ids, ["c-gate"])
        self.assertEqual(self.parse("person at the dock").camera_ids, ["c-dock1", "c-dock2"])
        self.assertEqual(self.parse("person in loading").camera_ids, ["c-dock1", "c-dock2"])
        self.assertEqual(self.parse("person outside the back door").camera_ids, ["c-back"])
        self.assertEqual(self.parse("person at the bakery").camera_ids, [])
        self.assertEqual(self.parse("person at the bakery").text, "person at the bakery")

    def test_where_in_the_frame_an_object_passed(self):
        self.assertEqual(self.parse("red car in the left half of the frame").zone, "left")
        self.assertEqual(self.parse("red car on the right side").zone, "right")
        self.assertEqual(self.parse("person in the centre").zone, "centre")
        self.assertEqual(self.parse("person in the center of the view").zone, "centre")
        self.assertEqual(self.parse("red car in the left half of the frame").text, "red car")
        for not_a_zone in ("man heading left", "man at the left", "car in the top"):
            self.assertIsNone(self.parse(not_a_zone).zone, not_a_zone)

    def test_words_about_the_object_are_never_taken_for_a_camera(self):
        cameras = {"a": "Car Port", "b": "Garden", "c": "Right Gate", "d": "Front Door", "e": "Garage"}
        for text, kept in (("white car after 9 pm", "white car"), ("person walking right", "person walking right"), ("person in front of the house", "person in front of the house")):
            parsed = semantic.parse(text, cameras, {}, now=self.NOW)
            self.assertEqual(parsed.camera_ids, [], text)
        self.assertEqual(semantic.parse("white car at the garden", cameras, {}, now=self.NOW).camera_ids, ["b"])
        self.assertEqual(semantic.parse("person at car port", cameras, {}, now=self.NOW).camera_ids, ["a"])  # The whole name still works

    def test_more_time_phrases(self):
        self.assertEqual(self.parse("man between 11 and 1 pm").time_of_day, (11 * 60, 13 * 60))
        self.assertEqual(self.parse("man from 6 pm to 8 pm").time_of_day, (18 * 60, 20 * 60))
        self.assertEqual(self.parse("man between 6 am and 8 am").time_of_day, (6 * 60, 8 * 60))
        for text in ("man since 3 days", "man after 2024", "man after 6 pmx"):
            self.assertIsNone(self.parse(text).time_of_day, text)
        yesterday_evening = self.parse("man yesterday evening")
        self.assertEqual(yesterday_evening.time_of_day, (17 * 60, 22 * 60))
        self.assertEqual(yesterday_evening.text, "man")

    def test_what_the_object_is_and_looks_like(self):
        walker = self.parse("a man in a red jacket and blue jeans heading left")
        self.assertEqual((walker.label, walker.facets), ("person", {"upper_colour": "red", "lower_colour": "blue", "direction": "left"}))
        truck = self.parse("grey truck driving away")
        self.assertEqual((truck.label, truck.facets), ("vehicle", {"vehicle_type": "truck", "vehicle_colour": "gray", "direction": "away"}))
        self.assertEqual(self.parse("someone in green").any_colours, ["green"])
        self.assertEqual(self.parse("parked car").facets["direction"], "stationary")
        self.assertEqual(self.parse("person towards the camera").facets["direction"], "toward")
        plain = self.parse("something odd")
        self.assertEqual((plain.label, plain.facets, plain.any_colours), (None, {}, []))


class AskCase(SearchCase):
    def setUp(self):
        super().setUp()
        self.model = fake_model()
        patch = mock.patch.object(clip, "shared_model", return_value=self.model)
        patch.start()
        self.addCleanup(patch.stop)

    def indexed(self, camera_id: str, label: str, at: datetime, colour: str, **attributes) -> str:
        track = self.add_track(camera_id, label, at, **attributes)
        with connect() as db:
            clip.store_embedding(db, track, self.model.model_id, self.model.embed_image(picture(colour), BOX))
        return track

    def ask(self, text: str, client=None, **params):
        response = (client or self.admin).get("/api/ai/search/ask", params={"q": text, **params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def order(self, body: dict) -> list[str]:
        return [item["track_id"] for item in body["results"]]


class RankingTests(AskCase):
    def setUp(self):
        super().setUp()
        at = NOW - timedelta(hours=6)
        self.white_van = self.indexed(self.d1, "vehicle", at, "white", vehicle_colour="white", vehicle_type="van")
        self.white_car = self.indexed(self.d1, "vehicle", at + timedelta(minutes=1), "white", vehicle_colour="white", vehicle_type="car")
        self.red_van = self.indexed(self.d2, "vehicle", at + timedelta(minutes=2), "red", vehicle_colour="red", vehicle_type="van")
        self.red_walker = self.indexed(self.d2, "person", at + timedelta(minutes=3), "red", upper="red", lower="black")
        self.blue_walker = self.indexed(self.y1, "person", at + timedelta(minutes=4), "blue", upper="blue", lower="blue")
        self.crowd = [self.indexed(self.y1, "vehicle", at + timedelta(minutes=10 + index), name, vehicle_colour=name, vehicle_type="car")
                      for index, name in enumerate(["black", "gray", "green", "yellow", "orange", "purple", "pink", "brown"])]

    def test_fixture_queries_rank_the_tracks_they_describe_in_the_top_five(self):
        for text, wanted in (("white van", self.white_van), ("a red van", self.red_van), ("man in a red jacket", self.red_walker), ("blue person", self.blue_walker),
                             ("yellow car", self.crowd[3]), ("pink car", self.crowd[6]), ("brown car", self.crowd[7])):
            body = self.ask(text)
            self.assertEqual(body["mode"], "image-text", text)
            self.assertIn(wanted, self.order(body)[:5], text)
        self.assertEqual(self.order(self.ask("white van"))[0], self.white_van)

    def test_a_result_has_a_score_a_rank_a_thumbnail_a_camera_and_a_time(self):
        body = self.ask("white van")
        first = body["results"][0]
        self.assertEqual((first["track_id"], first["rank"], first["camera_name"]), (self.white_van, 1, "Dock 1"))
        self.assertTrue(first["first_seen_at"] and first["thumbnail_url"])
        scores = [item["score"] for item in body["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(0.0 <= score <= 1.0 for score in scores))
        self.assertEqual(body["interpretation"]["text"], "white van")
        self.assertEqual(body["interpretation"]["label"], "vehicle")

    def test_what_a_description_names_narrows_the_kind_of_object(self):
        body = self.ask("red")
        self.assertEqual(body["interpretation"]["attributes"], {"any_colour": ["red"]})
        self.assertEqual({item["track_id"] for item in self.ask("red person")["results"]}, {self.red_walker, self.blue_walker})
        self.assertEqual({item["label"] for item in self.ask("red van")["results"]}, {"vehicle"})

    def test_no_answer_carries_an_embedding(self):
        text = json.dumps(self.ask("white van"))
        for word in ("vector", "embedding", "model_id", "clip:fixture"):
            self.assertNotIn(word, text)

    def test_the_limit_and_bad_requests(self):
        self.assertEqual(len(self.ask("car", limit=3)["results"]), 3)
        for params in ({"q": ""}, {"q": "   "}, {"q": "x" * 301}, {"q": "car", "limit": 0}, {"q": "car", "limit": 101}, {"q": "car", "utc_offset_minutes": 900},
                       {"q": "car", "start": "yesterday"}, {"q": "car", "start": NOW.isoformat(), "end": (NOW - DAY).isoformat()},
                       {"q": "car", "start": (NOW - 200 * DAY).isoformat(), "end": NOW.isoformat()}):
            self.assertEqual(self.admin.get("/api/ai/search/ask", params=params).status_code, 422, params)

    def test_tracks_never_indexed_are_counted_and_still_found_by_their_attributes(self):
        old = self.add_track(self.d1, "vehicle", NOW - timedelta(hours=7), vehicle_colour="white", vehicle_type="van")  # Analysed before the model was installed
        body = self.ask("white van")
        self.assertEqual(body["unindexed"], 1)
        self.assertIn(old, self.order(body))
        self.assertLess(self.order(body).index(self.white_van), self.order(body).index(old))


class NarrowingTests(AskCase):
    def setUp(self):
        super().setUp()
        base = NOW.replace(hour=12, minute=0, second=0) - DAY
        self.afternoon = self.indexed(self.d1, "vehicle", base + timedelta(hours=5), "white", vehicle_colour="white", vehicle_type="van")  # 17:00 yesterday
        self.evening = self.indexed(self.d1, "vehicle", base + timedelta(hours=7), "white", vehicle_colour="white", vehicle_type="van")  # 19:00 yesterday
        self.evening_yard = self.indexed(self.y1, "vehicle", base + timedelta(hours=7, minutes=5), "white", vehicle_colour="white", vehicle_type="van")
        self.two_days_ago = self.indexed(self.d1, "vehicle", base - DAY + timedelta(hours=7), "white", vehicle_colour="white", vehicle_type="van")

    def test_after_6_pm_keeps_only_the_evening_on_every_day(self):
        body = self.ask("white van after 6 pm")
        self.assertEqual(set(self.order(body)), {self.evening, self.evening_yard, self.two_days_ago})
        self.assertEqual(body["interpretation"]["time_of_day"], {"from": "18:00", "to": "24:00"})
        self.assertEqual(set(self.order(self.ask("white van before 6 pm"))), {self.afternoon})
        self.assertEqual(set(self.order(self.ask("white van between 4 and 6 pm"))), {self.afternoon})

    def test_the_time_of_day_is_the_viewers_local_time(self):
        body = self.ask("white van after 6 pm", utc_offset_minutes=-180)  # 19:00 UTC is 16:00 three hours behind
        self.assertEqual(set(self.order(body)), set())
        self.assertEqual(set(self.order(self.ask("white van between 3 and 5 pm", utc_offset_minutes=-180))), {self.evening, self.evening_yard, self.two_days_ago})

    def test_yesterday_narrows_the_range_and_says_so(self):
        body = self.ask("white van yesterday")
        self.assertEqual(set(self.order(body)), {self.afternoon, self.evening, self.evening_yard})
        self.assertIsNotNone(body["interpretation"]["range"])
        self.assertLess(body["start"], body["end"])
        self.assertEqual(set(self.order(self.ask("white van", start=(NOW - 3 * DAY).isoformat()))), {self.afternoon, self.evening, self.evening_yard, self.two_days_ago})

    def test_a_camera_or_group_named_in_the_description_narrows_the_cameras(self):
        self.assertEqual({item["camera_id"] for item in self.ask("white van at Yard 1")["results"]}, {self.y1})
        self.assertEqual({item["camera_id"] for item in self.ask("white van in dock")["results"]}, {self.d1})
        body = self.ask("white van on Dock 1 after 6 pm yesterday")
        self.assertEqual(set(self.order(body)), {self.evening})
        self.assertEqual([camera["name"] for camera in body["interpretation"]["cameras"]], ["Dock 1"])
        self.assertEqual(set(self.coverage_names(body)), {"Dock 1"})

    def coverage_names(self, body: dict) -> list[str]:
        return [camera["camera_name"] for camera in body["coverage"]["cameras"]]


class SiteClockTests(AskCase):
    def test_the_sites_time_zone_decides_what_a_time_of_day_means(self):
        track = self.indexed(self.d1, "vehicle", NOW.replace(hour=19, minute=0, second=0) - DAY, "white", vehicle_colour="white", vehicle_type="van")
        self.assertEqual(self.order(self.ask("white van after 6 pm")), [track])  # 19:00 UTC, and the site is on UTC
        with connect() as db:
            db.execute("UPDATE site_display_settings SET time_zone='America/Los_Angeles' WHERE singleton_id=1")
        local = (NOW.replace(hour=19, minute=0, second=0) - DAY).astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
        self.assertLess(local.hour, 18)  # Midday there
        self.assertEqual(self.order(self.ask("white van after 6 pm")), [])
        self.assertEqual(self.order(self.ask("white van after 6 pm", utc_offset_minutes=0)), [track])  # An explicit offset wins

    def test_a_time_that_has_not_happened_yet_has_nothing_to_look_at(self):
        body = self.ask("white van", start=(NOW + timedelta(hours=1)).isoformat(), end=(NOW + timedelta(hours=2)).isoformat())
        self.assertEqual((body["results"], body["not_yet"], body["coverage"]["analysed_percent"]), ([], True, None))


class ZoneTests(AskCase):
    def test_a_zone_phrase_keeps_only_objects_that_passed_through_it(self):
        at = NOW - timedelta(hours=2)
        left = self.indexed(self.d1, "vehicle", at, "white", vehicle_colour="white", vehicle_type="van")
        with connect() as db:
            db.execute("DELETE FROM ai_detections WHERE track_id=?", (left,))
        right = self.add_track(self.d1, "vehicle", at, vehicle_colour="white", vehicle_type="van", path=[(0.7, 0.5), (0.9, 0.5)])
        both = self.add_track(self.d1, "vehicle", at, vehicle_colour="white", vehicle_type="van", path=[(0.2, 0.5), (0.9, 0.5)])
        with connect() as db:
            db.execute("DELETE FROM ai_tracks WHERE id=?", (left,))
        left = self.add_track(self.d1, "vehicle", at, vehicle_colour="white", vehicle_type="van", path=[(0.1, 0.5), (0.3, 0.5)])
        body = self.ask("white van in the left half of the frame")
        self.assertEqual(set(self.order(body)), {left, both})
        self.assertEqual(body["interpretation"]["zone"], "left")
        self.assertEqual(set(self.order(self.ask("white van on the right side"))), {right, both})
        self.assertEqual(set(self.order(self.ask("white van"))), {left, right, both})


class AccessAndCoverageTests(AskCase):
    def setUp(self):
        super().setUp()
        at = NOW - timedelta(hours=4)
        self.dock = self.indexed(self.d1, "vehicle", at, "white", vehicle_colour="white", vehicle_type="van")
        self.yard = self.indexed(self.y1, "vehicle", at + timedelta(minutes=1), "white", vehicle_colour="white", vehicle_type="van")
        self.add_coverage(self.d1, "analyzed", NOW - DAY, NOW - timedelta(hours=3))
        self.add_coverage(self.d1, "failed", NOW - timedelta(hours=3), NOW - timedelta(hours=2), "Detector stopped")
        self.add_coverage(self.d1, "analyzed", NOW - timedelta(hours=2), NOW + timedelta(days=365))
        self.add_coverage(self.y1, "analyzed", NOW - DAY, NOW + timedelta(days=365))

    def test_someone_limited_to_the_dock_sees_only_dock_results_and_never_a_yard_camera_named_in_the_query(self):
        for text in ("white van", "white van at Yard 1", "white van in the yard"):
            body = self.ask(text, client=self.limited)
            self.assertEqual(self.order(body), [self.dock], text)
            self.assertEqual(body["interpretation"]["cameras"], [])
            self.assertEqual(set(self.coverage_names(body)), {"Dock 1", "Dock 2"})
        self.assertEqual(set(self.order(self.ask("white van at Yard 1"))), {self.yard})

    def coverage_names(self, body: dict) -> list[str]:
        return [camera["camera_name"] for camera in body["coverage"]["cameras"]]

    def test_an_answer_lists_the_time_that_was_not_analysed(self):
        body = self.ask("white van")
        self.assertFalse(body["coverage"]["fully_analysed"])
        dock = next(camera for camera in body["coverage"]["cameras"] if camera["camera_name"] == "Dock 1")
        self.assertTrue(any(gap["state"] == "failed" and gap["detail"] == "Detector stopped" for gap in dock["intervals"]), dock)
        self.assertIn("nothing was looked at", body["coverage_note"])

    def test_signing_in_is_required(self):
        from fastapi.testclient import TestClient
        with TestClient(self.app) as anonymous:
            self.assertEqual(anonymous.get("/api/ai/search/ask", params={"q": "van"}).status_code, 401)


class WithoutAModelTests(SearchCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(clip, "shared_model", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)
        at = NOW - timedelta(hours=3)
        self.red_blue = self.add_track(self.d1, "person", at, upper="red", lower="blue", direction="right")
        self.red_black = self.add_track(self.d1, "person", at + timedelta(minutes=1), upper="red", lower="black", direction="left")
        self.green = self.add_track(self.d2, "person", at + timedelta(minutes=2), upper="green", lower="black", direction="left")
        self.white_van = self.add_track(self.d1, "vehicle", at + timedelta(minutes=3), vehicle_colour="white", vehicle_type="van", direction="left")

    def ask(self, text: str, **params):
        response = self.admin.get("/api/ai/search/ask", params={"q": text, **params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_results_are_ranked_by_how_many_of_the_named_attributes_they_have(self):
        body = self.ask("man in a red jacket and blue jeans")
        self.assertEqual(body["mode"], "attributes")
        self.assertIn("No image-text model", body["mode_note"])
        self.assertEqual([item["track_id"] for item in body["results"]], [self.red_blue, self.red_black])
        self.assertEqual([item["score"] for item in body["results"]], [1.0, 0.5])
        self.assertEqual([item["track_id"] for item in self.ask("white van heading left")["results"]], [self.white_van])

    def test_a_description_that_names_nothing_lists_the_newest(self):
        body = self.ask("something odd at the gate")
        self.assertEqual(body["mode"], "browse")
        self.assertEqual(body["results"][0]["track_id"], self.white_van)
        self.assertIsNone(body["results"][0]["score"])


class NothingLeavesTheServerTests(AskCase):
    def test_no_network_request_is_made_when_indexing_or_searching(self):
        def refuse(*args, **kwargs):
            raise AssertionError("A network request was made")
        with mock.patch.object(socket.socket, "connect", refuse), mock.patch.object(socket, "create_connection", refuse), \
                mock.patch.object(socket, "getaddrinfo", refuse), mock.patch.object(socket.socket, "sendto", refuse):
            track = self.indexed(self.d1, "vehicle", NOW - timedelta(hours=1), "white", vehicle_colour="white", vehicle_type="van")
            body = self.ask("white van at Dock 1 after 6 pm yesterday")
            self.assertEqual(body["mode"], "image-text")
            self.assertEqual(self.ask("white van")["results"][0]["track_id"], track)


class RetentionTests(AskCase):
    def test_deleting_footage_removes_the_embeddings(self):
        track = self.indexed(self.d1, "vehicle", NOW - timedelta(hours=1), "white", vehicle_colour="white", vehicle_type="van")
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM track_image_embeddings WHERE track_id=?", (track,)).fetchone()[0], 1)
            db.execute("DELETE FROM ai_tracks WHERE camera_id=?", (self.d1,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM track_image_embeddings").fetchone()[0], 0)

    def test_only_embeddings_from_the_selected_model_are_compared(self):
        track = self.add_track(self.d1, "vehicle", NOW - timedelta(hours=1), vehicle_colour="white", vehicle_type="van")
        other = fake_model("clip:another")
        with connect() as db:
            clip.store_embedding(db, track, other.model_id, other.embed_image(picture("white"), BOX))
        body = self.ask("white van")
        self.assertEqual(body["unindexed"], 1)


class WorkerIndexesTests(SceneTestCase):
    def test_the_worker_embeds_each_track_once_with_the_selected_model(self):
        model = fake_model()
        with mock.patch.object(clip, "shared_model", return_value=model):
            self.analyse_footage()
        found = self.client.get("/api/ai/tracks", params=self.scene_range()).json()["tracks"]
        self.assertTrue(found)
        with connect() as db:
            stored = {row["track_id"]: row for row in db.execute("SELECT track_id,model_id,dim FROM track_image_embeddings")}
        self.assertEqual(set(stored), {track["id"] for track in found})
        self.assertEqual({(row["model_id"], row["dim"]) for row in stored.values()}, {("clip:fixture", len(PALETTE))})

    def test_without_a_model_nothing_is_indexed_and_detection_is_unaffected(self):
        with mock.patch.object(clip, "shared_model", return_value=None):
            self.analyse_footage()
        self.assertTrue(self.client.get("/api/ai/tracks", params=self.scene_range()).json()["tracks"])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM track_image_embeddings").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
