"""Attribute search across cameras (#125): what the worker stores with each track, and finding tracks by it, with coverage."""
from __future__ import annotations

import json
import random
import subprocess
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from app import attributes, search
from app.store import connect
from tests.fixtures import detection_scene as scene
from tests.scene_case import SceneTestCase
from tests.test_access_grants import GrantCase

NOW = datetime.now(timezone.utc).replace(microsecond=0)
DAY = timedelta(days=1)


def block(b: int, g: int, r: int, width: int = 40, height: int = 40) -> np.ndarray:
    return np.tile(np.array([[b, g, r]], dtype=np.uint8), (width * height, 1))


def frame_with(regions: list[tuple[tuple[int, int, int, int], tuple[int, int, int]]]) -> bytes:
    """A grey detector-sized frame with flat rectangles ((x1, y1, x2, y2), (b, g, r))."""
    image = np.empty((scene.HEIGHT, scene.WIDTH, 3), dtype=np.uint8)
    image[:, :] = scene.BACKGROUND
    for (x1, y1, x2, y2), colour in regions:
        image[y1:y2, x1:x2] = colour
    return image.tobytes()


def box_of(x1: int, y1: int, x2: int, y2: int) -> dict[str, float]:
    return {"x": x1 / scene.WIDTH, "y": y1 / scene.HEIGHT, "width": (x2 - x1) / scene.WIDTH, "height": (y2 - y1) / scene.HEIGHT}


class PaletteTests(unittest.TestCase):
    def test_flat_colours_fall_into_their_palette_names(self):
        flat = {"red": (30, 30, 220), "green": (30, 200, 30), "blue": (220, 40, 30), "black": (10, 10, 10), "white": (240, 240, 240), "gray": (120, 120, 120),
                "yellow": (20, 220, 230), "orange": (20, 140, 250), "purple": (200, 40, 150), "pink": (190, 120, 250), "brown": (30, 60, 110)}
        for name, (b, g, r) in flat.items():
            self.assertEqual(attributes.classify_pixels(block(b, g, r)), name, name)
        self.assertEqual(set(flat), set(attributes.PALETTE))
        self.assertIsNone(attributes.classify_pixels(np.empty((0, 3), dtype=np.uint8)))

    def test_the_dominant_colour_wins_over_a_little_of_another(self):
        pixels = np.concatenate([block(30, 30, 220, 30, 30), block(220, 40, 30, 10, 10)])
        self.assertEqual(attributes.classify_pixels(pixels), "red")

    def test_a_person_has_an_upper_and_a_lower_colour_and_a_vehicle_one_body_colour(self):
        # A red top over blue trousers, within the person's box.
        person = frame_with([((100, 40, 124, 72), (30, 30, 220)), ((100, 72, 124, 104), (220, 40, 30))])
        colours = attributes.describe_colours(person, "person", box_of(100, 40, 124, 104))
        self.assertEqual(colours, {"upper_colour": "red", "lower_colour": "blue"})
        van = frame_with([((200, 150, 290, 190), (30, 200, 30))])
        self.assertEqual(attributes.describe_colours(van, "vehicle", box_of(200, 150, 290, 190)), {"vehicle_colour": "green"})
        self.assertEqual(attributes.describe_colours(van, "bicycle", box_of(200, 150, 290, 190)), {})

    def test_a_frame_that_is_not_detector_sized_describes_nothing(self):
        self.assertEqual(attributes.describe_colours(b"short", "person", box_of(10, 10, 30, 60)), {})
        self.assertEqual(attributes.describe_colours(None, "vehicle", box_of(10, 10, 30, 60)), {})


class GeometryTests(unittest.TestCase):
    def test_direction_is_where_the_track_went_across_the_frame(self):
        cases = {((0.1, 0.5), (0.8, 0.52)): "right", ((0.9, 0.5), (0.2, 0.45)): "left", ((0.5, 0.2), (0.52, 0.8)): "toward", ((0.5, 0.8), (0.5, 0.2)): "away",
                 ((0.5, 0.5), (0.53, 0.52)): "stationary"}
        for (begin, finish), expected in cases.items():
            self.assertEqual(attributes.direction_of(begin, finish), expected, (begin, finish))

    def test_a_zone_touches_the_cells_it_overlaps_and_a_box_sits_in_one_cell(self):
        left = attributes.zone_cells(search.ZONE_PRESETS["left"])
        right = attributes.zone_cells(search.ZONE_PRESETS["right"])
        self.assertEqual(left & right, 0)
        self.assertEqual(bin(left).count("1"), 18)
        self.assertTrue(attributes.cell_mask({"x": 0.05, "y": 0.05, "width": 0.1, "height": 0.1}) & left)
        self.assertFalse(attributes.cell_mask({"x": 0.8, "y": 0.5, "width": 0.1, "height": 0.1}) & left)
        self.assertEqual(bin(attributes.cell_mask({"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0})).count("1"), 1)

    def test_zones_are_presets_or_rectangles_and_must_fit_the_frame(self):
        self.assertEqual(search.parse_zone("left"), search.ZONE_PRESETS["left"])
        self.assertEqual(search.parse_zone("0.1,0.2,0.5,0.4"), {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.4})
        for bad in ("middle", "1,2,3", "0.6,0,0.6,1", "-0.1,0,0.5,0.5", "0,0,0,1", "a,b,c,d"):
            with self.assertRaises(ValueError, msg=bad):
                search.parse_zone(bad)


class ProfileTests(unittest.TestCase):
    def test_a_track_keeps_what_most_detections_agreed_on_and_the_earlier_of_a_tie(self):
        profile = attributes.TrackProfile()
        box = {"x": 0.1, "y": 0.4, "width": 0.05, "height": 0.2}
        for colour in ("red", "red", "blue"):
            profile.add({"upper_colour": colour, "lower_colour": "black"}, box)
        self.assertEqual((profile.summary()["upper_colour"], profile.summary()["lower_colour"], profile.summary()["vehicle_type"]), ("red", "black", None))
        tied = attributes.TrackProfile()
        for colour in ("green", "blue", "blue", "green"):
            tied.add({"vehicle_colour": colour}, box)
        self.assertEqual(tied.summary()["vehicle_colour"], "green")

    def test_direction_and_grid_follow_the_path(self):
        profile = attributes.TrackProfile()
        for x in (0.05, 0.3, 0.6, 0.85):
            profile.add({}, {"x": x, "y": 0.4, "width": 0.05, "height": 0.2})
        summary = profile.summary()
        self.assertEqual(summary["direction"], "right")
        self.assertGreater(bin(summary["zone_mask"]).count("1"), 2)


class FakeCore:
    """An OpenVINO core whose attribute model answers fixed scores."""

    def __init__(self, colour: int, kind: int) -> None:
        self.colour, self.kind = colour, kind
        self.seen_shape = None

    def read_model(self, path: str):
        return path

    def compile_model(self, model, provider):
        core = self

        class Output:
            def __init__(self, name: str) -> None:
                self.name = name

            def get_any_name(self) -> str:
                return self.name

        def compiled(tensor):
            core.seen_shape = tensor.shape
            colour = np.zeros((1, 7, 1, 1), dtype=np.float32)
            colour[0, core.colour] = 1
            kind = np.zeros((1, 4, 1, 1), dtype=np.float32)
            kind[0, core.kind] = 1
            return {Output("color"): colour, Output("type"): kind}
        return compiled


class AttributeModelTests(unittest.TestCase):
    def test_the_vehicle_model_gives_a_colour_and_a_type_from_a_72_pixel_crop(self):
        core = FakeCore(colour=3, kind=2)  # red, truck
        model = attributes.VehicleAttributeModel(Path("/models/vehicle.xml"), core=core)
        frame = frame_with([((200, 150, 290, 190), (30, 30, 220))])
        self.assertEqual(model.classify(frame, box_of(200, 150, 290, 190)), {"vehicle_colour": "red", "vehicle_type": "truck"})
        self.assertEqual(core.seen_shape, (1, 3, 72, 72))

    def test_the_model_is_used_for_vehicles_only_and_a_failing_one_leaves_the_pixel_colours(self):
        model = attributes.VehicleAttributeModel(Path("/models/vehicle.xml"), core=FakeCore(colour=5, kind=1))
        frame = frame_with([((200, 150, 290, 190), (30, 200, 30)), ((100, 40, 124, 104), (30, 30, 220))])
        detections = [{"label": "vehicle", "box": box_of(200, 150, 290, 190)}, {"label": "person", "box": box_of(100, 40, 124, 104)}]
        attributes.describe(frame, detections, model)
        self.assertEqual(detections[0]["attributes"], {"vehicle_colour": "blue", "vehicle_type": "bus"})  # The model's colour wins over the pixels'.
        self.assertEqual(set(detections[1]["attributes"]), {"upper_colour", "lower_colour"})

        class Broken:
            def classify(self, frame, box):
                raise RuntimeError("model fault")
        again = [{"label": "vehicle", "box": box_of(200, 150, 290, 190)}]
        attributes.describe(frame, again, Broken())
        self.assertEqual(again[0]["attributes"], {"vehicle_colour": "green"})

    def test_without_the_model_files_there_is_no_model_and_no_error(self):
        self.assertIsNone(attributes.load_attribute_model(Path("/nowhere/vehicle.xml")))
        self.assertFalse(attributes.attribute_model_available(Path("/nowhere/vehicle.xml")))
        with tempfile.TemporaryDirectory() as directory:
            xml = Path(directory, "vehicle.xml")
            xml.write_text("<net/>")
            self.assertFalse(attributes.attribute_model_available(xml))  # The weights are missing too.
            xml.with_suffix(".bin").write_bytes(b"\0")
            self.assertTrue(attributes.attribute_model_available(xml))
            with mock.patch.object(attributes, "VehicleAttributeModel", side_effect=RuntimeError("cannot read the model")):
                self.assertIsNone(attributes.load_attribute_model(xml))  # Present but unloadable: detection goes on without it.


class WorkerStoresAttributesTests(SceneTestCase):
    def test_the_worker_stores_each_tracks_colours_and_direction(self):
        self.analyse_footage()
        tracks = self.client.get("/api/ai/tracks", params=self.scene_range()).json()["tracks"]
        by_colour = {}
        for track in tracks:
            detail = self.client.get(f"/api/ai/tracks/{track['id']}").json()
            by_colour[detail["detections"][0]["attributes"]["color"]] = track
        red, green, blue = by_colour["red"], by_colour["green"], by_colour["blue"]
        self.assertEqual(red["attributes"], {"upper_colour": "red", "lower_colour": "red", "direction": "right"})
        self.assertEqual(green["attributes"], {"upper_colour": "green", "lower_colour": "green", "direction": "left"})
        self.assertEqual(blue["attributes"], {"vehicle_colour": "blue", "direction": "left"})  # No type: no attribute model is installed.

    def test_the_worker_search_finds_the_scene_by_its_attributes(self):
        self.analyse_footage()
        window = {"start": (self.scene_start - timedelta(minutes=5)).isoformat(), "end": (self.scene_start + timedelta(minutes=5)).isoformat()}

        def found(**filters) -> list[str]:
            response = self.client.get("/api/ai/search", params={**window, **filters})
            self.assertEqual(response.status_code, 200, response.text)
            return sorted(item["attributes"].get("upper_colour") or item["attributes"]["vehicle_colour"] for item in response.json()["results"])
        self.assertEqual(found(), ["blue", "green", "red"])
        self.assertEqual(found(label="person"), ["green", "red"])
        self.assertEqual(found(label="person", direction="right"), ["red"])
        self.assertEqual(found(upper_colour="green"), ["green"])
        self.assertEqual(found(direction="left"), ["blue", "green"])
        self.assertEqual(found(vehicle_colour="blue"), ["blue"])
        self.assertEqual(found(label="vehicle", direction="right"), [])
        # The red person starts at the left and ends at the right; the vehicle drives along the lower path.
        self.assertEqual(found(zone="bottom", label="person"), [])
        self.assertEqual(found(zone="bottom"), ["blue"])


class SearchCase(GrantCase):
    """Cameras Dock 1, Dock 2 (group Dock), Yard 1 (group Yard), Loose; an operator limited to Dock; fixture tracks with known attributes."""

    def setUp(self):
        super().setUp()
        self.names = {self.d1: "Dock 1", self.d2: "Dock 2", self.y1: "Yard 1", self.loose: "Loose"}

    def add_track(self, camera_id: str, label: str, at: datetime, *, upper: str | None = None, lower: str | None = None, vehicle_colour: str | None = None,
                  vehicle_type: str | None = None, direction: str | None = None, path: list[tuple[float, float]] | None = None, seconds: int = 10, keyframe: bool = True) -> str:
        track = str(uuid.uuid4())
        path = path or [(0.2, 0.5), (0.7, 0.5)]
        mask = 0
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,?,?,?,'m','1','CPU')",
                       (track, camera_id, label, at.isoformat(), at.isoformat()))
            for index, (cx, cy) in enumerate(path):
                box = {"x": cx - 0.025, "y": cy - 0.1, "width": 0.05, "height": 0.2}
                mask |= attributes.cell_mask(box)
                db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                           "VALUES(?,?,?,?,0.9,?,?,?,?,'m','1','CPU')", (camera_id, track, (at + timedelta(seconds=seconds * index / max(1, len(path) - 1))).isoformat(), label,
                                                                   box["x"], box["y"], box["width"], box["height"]))
            db.execute("UPDATE ai_tracks SET upper_colour=?,lower_colour=?,vehicle_colour=?,vehicle_type=?,direction=?,zone_mask=? WHERE id=?",
                       (upper, lower, vehicle_colour, vehicle_type, direction, mask, track))
            if keyframe:
                db.execute("INSERT INTO track_keyframes(track_id,captured_at,jpeg) VALUES(?,?,?)", (track, at.isoformat(), b"\xff\xd8fixture"))
        return track

    def add_coverage(self, camera_id: str, state: str, start: datetime, end: datetime | None, detail: str = "") -> None:
        with connect() as db:
            db.execute("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at,detail) VALUES(?,?,?,?,?)",
                       (camera_id, state, start.isoformat(), end.isoformat() if end else None, detail))

    def run_search(self, client: TestClient | None = None, **params) -> dict:
        query = {"start": (NOW - 2 * DAY).isoformat(), "end": NOW.isoformat(), **params}
        response = (client or self.admin).get("/api/ai/search", params=query)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def ids(self, body: dict) -> set[str]:
        return {item["track_id"] for item in body["results"]}


class FilterTests(SearchCase):
    def setUp(self):
        super().setUp()
        at = NOW - timedelta(hours=3)
        self.red_walker = self.add_track(self.d1, "person", at, upper="red", lower="blue", direction="right")
        self.red_standing = self.add_track(self.d1, "person", at + timedelta(minutes=1), upper="red", lower="black", direction="stationary")
        self.blue_walker = self.add_track(self.d2, "person", at + timedelta(minutes=2), upper="blue", lower="blue", direction="left")
        self.white_car = self.add_track(self.d1, "vehicle", at + timedelta(minutes=3), vehicle_colour="white", vehicle_type="car", direction="left")
        self.red_truck = self.add_track(self.y1, "vehicle", at + timedelta(minutes=4), vehicle_colour="red", vehicle_type="truck", direction="right")
        self.untagged = self.add_track(self.d2, "person", at + timedelta(minutes=5))  # Recorded before attributes existed.
        self.everything = {self.red_walker, self.red_standing, self.blue_walker, self.white_car, self.red_truck, self.untagged}

    def test_with_no_filter_every_track_in_range_is_found_in_time_order(self):
        body = self.run_search()
        self.assertEqual([item["track_id"] for item in body["results"]],
                         [self.red_walker, self.red_standing, self.blue_walker, self.white_car, self.red_truck, self.untagged])
        self.assertEqual([item["track_id"] for item in self.run_search(order="desc")["results"]][0], self.untagged)

    def test_each_filter_returns_the_matching_tracks_and_not_the_others(self):
        cases = [
            ({"label": "person"}, {self.red_walker, self.red_standing, self.blue_walker, self.untagged}),
            ({"label": "vehicle"}, {self.white_car, self.red_truck}),
            ({"upper_colour": "red"}, {self.red_walker, self.red_standing}),
            ({"lower_colour": "blue"}, {self.red_walker, self.blue_walker}),
            ({"upper_colour": "red", "lower_colour": "blue"}, {self.red_walker}),
            ({"vehicle_colour": "red"}, {self.red_truck}),
            ({"vehicle_type": "car"}, {self.white_car}),
            ({"vehicle_type": "truck", "vehicle_colour": "white"}, set()),
            ({"direction": "left"}, {self.blue_walker, self.white_car}),
            ({"direction": "right", "label": "vehicle"}, {self.red_truck}),
            ({"direction": "stationary"}, {self.red_standing}),
            ({"camera_ids": [self.d2]}, {self.blue_walker, self.untagged}),
            ({"camera_ids": [self.d1, self.y1], "label": "vehicle"}, {self.white_car, self.red_truck}),
        ]
        for params, expected in cases:
            self.assertEqual(self.ids(self.run_search(**params)), expected, params)

    def test_tracks_are_found_by_the_time_they_were_seen(self):
        at = NOW - timedelta(hours=3)
        window = lambda start, end: self.ids(self.run_search(start=(at + timedelta(minutes=start)).isoformat(), end=(at + timedelta(minutes=end)).isoformat()))  # noqa: E731
        self.assertEqual(window(2, 3.5), {self.blue_walker, self.white_car})
        self.assertEqual(window(-60, -1), set())
        self.assertEqual(window(0, 0.5), {self.red_walker})
        # A track still in view at the start of the range overlaps it.
        self.assertIn(self.red_walker, window(0.1, 0.5))

    def test_a_zone_finds_the_tracks_that_passed_through_it(self):
        crosser = self.add_track(self.d1, "person", NOW - timedelta(hours=2), upper="green", path=[(0.1, 0.6), (0.5, 0.6), (0.9, 0.6)])
        top = self.add_track(self.d1, "person", NOW - timedelta(hours=2), upper="green", path=[(0.2, 0.1), (0.4, 0.15)])
        found = lambda zone: self.ids(self.run_search(zone=zone, upper_colour="green"))  # noqa: E731
        self.assertEqual(found("right"), {crosser})
        self.assertEqual(found("left"), {crosser, top})
        self.assertEqual(found("top"), {top})
        self.assertEqual(found("bottom"), {crosser})
        # Inside the grid cell the crosser's first detection is in, but not on its centre: the cell is touched, and the exact check says no.
        self.assertEqual(found("0.12,0.5,0.04,0.2"), set())
        self.assertEqual(found("0.08,0.5,0.04,0.2"), {crosser})

    def test_results_say_what_they_are_and_where_the_picture_is(self):
        (item,) = [item for item in self.run_search(vehicle_type="truck")["results"]]
        self.assertEqual((item["camera_id"], item["camera_name"], item["label"]), (self.y1, "Yard 1", "vehicle"))
        self.assertEqual(item["attributes"], {"vehicle_colour": "red", "vehicle_type": "truck", "direction": "right"})
        self.assertEqual(item["thumbnail_url"], f"/api/ai/tracks/{self.red_truck}/thumbnail")
        self.assertAlmostEqual(item["box"]["width"], 0.05)

    def test_a_search_is_limited_and_says_when_there_are_more(self):
        body = self.run_search(limit=2)
        self.assertEqual((len(body["results"]), body["truncated"]), (2, True))
        self.assertEqual([item["track_id"] for item in body["results"]], [self.red_walker, self.red_standing])
        self.assertFalse(self.run_search(limit=6)["truncated"])

    def test_bad_filters_and_ranges_are_refused(self):
        window = {"start": (NOW - DAY).isoformat(), "end": NOW.isoformat()}
        for params in ({"upper_colour": "chartreuse"}, {"vehicle_type": "tank"}, {"direction": "sideways"}, {"label": "dog"}, {"zone": "everywhere"}, {"order": "random"},
                       {"limit": 0}, {"limit": 201}, {"start": "yesterday"}, {"start": NOW.isoformat(), "end": (NOW - DAY).isoformat()},
                       {"start": (NOW - timedelta(days=93)).isoformat()}, {"start": "2026-06-01T00:00:00", "end": "2026-06-02T00:00:00"}):
            response = self.admin.get("/api/ai/search", params={**window, **params})
            self.assertEqual(response.status_code, 422, params)

    def test_options_list_the_filters_and_say_whether_vehicle_type_is_available(self):
        body = self.admin.get("/api/ai/search/options").json()
        self.assertEqual(body["colours"], list(attributes.PALETTE))
        self.assertEqual((body["vehicle_types"], body["directions"]), (list(attributes.VEHICLE_TYPES), list(attributes.DIRECTIONS)))
        self.assertFalse(body["vehicle_type_available"])
        with mock.patch("app.search.attribute_model_available", return_value=True):
            self.assertTrue(self.admin.get("/api/ai/search/options").json()["vehicle_type_available"])


class AccessTests(SearchCase):
    def test_an_operator_searches_only_the_cameras_they_may_see(self):
        at = NOW - timedelta(hours=1)
        mine = self.add_track(self.d1, "person", at, upper="red")
        hidden = self.add_track(self.y1, "person", at, upper="red")
        loose = self.add_track(self.loose, "person", at, upper="red")
        self.assertEqual(self.ids(self.run_search(upper_colour="red")), {mine, hidden, loose})
        self.assertEqual(self.ids(self.run_search(self.limited, upper_colour="red")), {mine})
        # Asking for a hidden camera by name finds nothing and does not say it exists.
        asked = self.run_search(self.limited, upper_colour="red", camera_ids=[self.y1, self.d1])
        self.assertEqual(self.ids(asked), {mine})
        nothing = self.run_search(self.limited, camera_ids=[self.y1, "no-such-camera"])
        self.assertEqual((nothing["results"], nothing["coverage"]["searched_cameras"]), ([], 0))

    def test_coverage_names_only_the_cameras_searched(self):
        body = self.run_search(self.limited)
        self.assertEqual(body["coverage"]["searched_cameras"], 2)
        self.assertEqual({item["camera_name"] for item in body["coverage"]["cameras"]}, {"Dock 1", "Dock 2"})

    def test_a_thumbnail_of_a_hidden_camera_does_not_exist_for_them(self):
        track = self.add_track(self.y1, "person", NOW - timedelta(hours=1))
        self.assertEqual(self.admin.get(f"/api/ai/tracks/{track}/thumbnail").status_code, 200)
        self.assertEqual(self.limited.get(f"/api/ai/tracks/{track}/thumbnail").status_code, 404)
        self.assertEqual(self.limited.get(f"/api/ai/tracks/{uuid.uuid4()}/thumbnail").status_code, 404)

    def test_tokens_need_the_read_scope_and_signing_in_is_required(self):
        track = self.add_track(self.d1, "person", NOW - timedelta(hours=1), upper="red")
        params = {"start": (NOW - DAY).isoformat(), "end": NOW.isoformat(), "upper_colour": "red"}

        def token(scopes: list[str]) -> dict[str, str]:
            created = self.admin.post("/api/admin/api-tokens", json={"name": f"search-{uuid.uuid4().hex[:6]}", "scopes": scopes})
            self.assertEqual(created.status_code, 201, created.text)
            return {"Authorization": f"Bearer {created.json()['secret']}"}
        anonymous = TestClient(self.app)
        self.assertEqual(anonymous.get("/api/ai/search", params=params).status_code, 401)
        self.assertEqual(anonymous.get(f"/api/ai/tracks/{track}/thumbnail").status_code, 401)
        allowed = anonymous.get("/api/ai/search", params=params, headers=token(["read"]))
        self.assertEqual((allowed.status_code, [item["track_id"] for item in allowed.json()["results"]]), (200, [track]))
        for scopes in (["metrics"], ["media"]):
            denied = anonymous.get("/api/ai/search", params=params, headers=token(scopes))
            self.assertEqual(denied.status_code, 403, scopes)
            self.assertEqual(anonymous.get(f"/api/ai/tracks/{track}/thumbnail", headers=token(scopes)).status_code, 403, scopes)


class CoverageTests(SearchCase):
    def window(self, hours: float = 10) -> dict:
        return {"start": (NOW - timedelta(hours=hours)).isoformat(), "end": NOW.isoformat()}

    def coverage(self, **params) -> dict:
        return self.run_search(**self.window(), **params)["coverage"]

    def test_unanalysed_intervals_are_listed_with_the_results_and_say_why(self):
        hour = timedelta(hours=1)
        # Dock 1: analysed except a failure, a delay and an hour when the AI was not running at all.
        self.add_coverage(self.d1, "analyzed", NOW - 10 * hour, NOW - 6 * hour)
        self.add_coverage(self.d1, "failed", NOW - 6 * hour, NOW - 5 * hour, "Camera stream ended")
        self.add_coverage(self.d1, "analyzed", NOW - 5 * hour, NOW - 3 * hour)
        self.add_coverage(self.d1, "delayed", NOW - 3 * hour, NOW - 2 * hour, "Inference was delayed")
        self.add_coverage(self.d1, "analyzed", NOW - 1 * hour, None)
        # Dock 2 is analysed throughout; Yard 1 and Loose have never been analysed.
        self.add_coverage(self.d2, "analyzed", NOW - 20 * hour, None)
        body = self.coverage()
        by_name = {item["camera_name"]: item for item in body["cameras"]}
        self.assertNotIn("Dock 2", by_name)
        self.assertEqual(set(by_name), {"Dock 1", "Yard 1", "Loose"})
        self.assertFalse(body["fully_analysed"])
        dock = by_name["Dock 1"]
        self.assertEqual([(item["state"], item["seconds"]) for item in dock["intervals"]], [("failed", 3600.0), ("delayed", 3600.0), ("not_running", 3600.0)])
        self.assertEqual(dock["intervals"][0]["detail"], "Camera stream ended")
        self.assertEqual(dock["intervals"][2]["detail"], "AI analysis was not running")
        self.assertEqual((dock["analysed_seconds"], dock["unanalysed_seconds"]), (7 * 3600.0, 3 * 3600.0))
        never = by_name["Yard 1"]
        self.assertEqual((never["analysed_seconds"], [item["state"] for item in never["intervals"]], never["unanalysed_seconds"]), (0.0, ["not_running"], 10 * 3600.0))
        self.assertEqual(body["analysed_percent"], round(100 * (7 + 10) * 3600 / (10 * 3600 * 4), 1))

    def test_a_range_that_is_analysed_throughout_has_no_unanalysed_time(self):
        for camera in (self.d1, self.d2, self.y1, self.loose):
            self.add_coverage(camera, "analyzed", NOW - timedelta(days=3), None)
        body = self.coverage()
        self.assertEqual((body["fully_analysed"], body["analysed_percent"], body["cameras"]), (True, 100.0, []))

    def test_coverage_that_ends_before_the_range_or_starts_after_it_does_not_count(self):
        self.add_coverage(self.d1, "analyzed", NOW - timedelta(days=5), NOW - timedelta(hours=11))
        self.add_coverage(self.d1, "analyzed", NOW + timedelta(hours=1), None)
        item = next(entry for entry in self.coverage()["cameras"] if entry["camera_name"] == "Dock 1")
        self.assertEqual(item["analysed_seconds"], 0.0)

    def test_no_match_is_never_silent_about_time_that_was_not_looked_at(self):
        body = self.run_search(**self.window(), upper_colour="red")
        self.assertEqual(body["results"], [])
        self.assertEqual(body["coverage"]["searched_cameras"], 4)
        self.assertEqual(len(body["coverage"]["cameras"]), 4)
        self.assertIn("unanalysed time is listed", body["coverage_note"])

    def test_many_intervals_are_capped_per_camera_and_counted(self):
        for index in range(30):
            start = NOW - timedelta(hours=10) + timedelta(minutes=index * 10)
            self.add_coverage(self.d1, "analyzed", start, start + timedelta(minutes=5))
        item = next(entry for entry in self.coverage()["cameras"] if entry["camera_name"] == "Dock 1")
        self.assertEqual(len(item["intervals"]), search.MAX_INTERVALS_PER_CAMERA)
        self.assertGreater(item["more_intervals"], 0)

    def test_the_future_is_not_counted_as_unanalysed(self):
        self.add_coverage(self.d1, "analyzed", NOW - timedelta(hours=2), None)
        body = self.run_search(start=(NOW - timedelta(hours=1)).isoformat(), end=(NOW + timedelta(days=1)).isoformat())["coverage"]
        self.assertFalse(any(entry["camera_name"] == "Dock 1" for entry in body["cameras"]))


class ThumbnailTests(SearchCase):
    def decode(self, jpeg: bytes) -> np.ndarray:
        result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "image2pipe", "-i", "pipe:0", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
                                input=jpeg, capture_output=True, check=True)
        return np.frombuffer(result.stdout, dtype=np.uint8)

    def keyframe_track(self) -> str:
        from app.tracks import encode_jpeg
        jpeg = encode_jpeg(scene.frame(0))
        self.assertIsNotNone(jpeg)
        track = self.add_track(self.d1, "person", NOW - timedelta(hours=1), keyframe=False)
        with connect() as db:
            db.execute("INSERT INTO track_keyframes(track_id,captured_at,jpeg) VALUES(?,?,?)", (track, (NOW - timedelta(hours=1)).isoformat(), jpeg))
        return track

    def test_the_thumbnail_is_the_stored_frame_and_is_cached_privately(self):
        track = self.keyframe_track()
        response = self.admin.get(f"/api/ai/tracks/{track}/thumbnail")
        self.assertEqual((response.status_code, response.headers["content-type"]), (200, "image/jpeg"))
        self.assertIn("private", response.headers["cache-control"])
        self.assertGreater(float(self.decode(response.content).mean()), 60)

    def test_a_privacy_mask_is_drawn_over_the_thumbnail(self):
        track = self.keyframe_track()
        with mock.patch("app.privacy.server_zones", return_value=[{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}]):
            response = self.admin.get(f"/api/ai/tracks/{track}/thumbnail")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertLess(float(self.decode(response.content).mean()), 30)  # Everything the mask covers is black (the picture is mid-grey without it).

    def test_if_the_mask_cannot_be_drawn_there_is_no_picture(self):
        track = self.keyframe_track()
        with mock.patch("app.privacy.server_zones", return_value=[{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}]), \
                mock.patch("app.privacy.mask_jpeg", side_effect=RuntimeError("FFmpeg could not apply the privacy mask")):
            response = self.admin.get(f"/api/ai/tracks/{track}/thumbnail")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"\xff\xd8", response.content)

    def test_a_track_without_a_keyframe_has_no_thumbnail(self):
        track = self.add_track(self.d1, "person", NOW - timedelta(hours=1), keyframe=False)
        self.assertEqual(self.admin.get(f"/api/ai/tracks/{track}/thumbnail").status_code, 404)
        (item,) = self.run_search()["results"]
        self.assertIsNone(item["thumbnail_url"])


class ScaleTests(unittest.TestCase):
    """30 days of detections for 50 cameras: a search answers in under two seconds on the CI runner (a measurement, not a deployment claim)."""

    CAMERAS = 50
    DAYS = 30
    TRACKS_PER_CAMERA_DAY = 100
    BUDGET_SECONDS = 2.0

    @classmethod
    def setUpClass(cls):
        import os
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        cls.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")}
        os.environ.update({"NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_RECORDINGS_PATH": str(root / "recordings"), "NVR_CREDENTIAL_KEY_PATH": str(root / "credentials.key")})
        (root / "recordings").mkdir()
        from app.store import initialize
        initialize()
        rng = random.Random(125)
        cls.cameras = [str(uuid.uuid4()) for _ in range(cls.CAMERAS)]
        cls.end = datetime.now(timezone.utc).replace(microsecond=0)
        cls.start = cls.end - timedelta(days=cls.DAYS)
        from app.credentials import encrypt
        tracks, detections = [], []
        with connect() as db:
            for number, camera in enumerate(cls.cameras):
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (camera, f"Camera {number:02d}", encrypt("rtsp://camera.invalid/main")))
                db.execute("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at) VALUES(?,'analyzed',?,NULL)", (camera, (cls.start - timedelta(days=1)).isoformat()))
                for day in range(cls.DAYS):
                    for _ in range(cls.TRACKS_PER_CAMERA_DAY):
                        seen = cls.start + timedelta(days=day, seconds=rng.randrange(86_000))
                        person = rng.random() < 0.7
                        track = str(uuid.uuid4())
                        x0, x1 = rng.random() * 0.4, 0.5 + rng.random() * 0.45
                        y = 0.2 + rng.random() * 0.6
                        mask = attributes.cell_mask({"x": x0, "y": y - 0.1, "width": 0.05, "height": 0.2}) | attributes.cell_mask({"x": x1, "y": y - 0.1, "width": 0.05, "height": 0.2})
                        tracks.append((track, camera, "person" if person else "vehicle", seen.isoformat(), (seen + timedelta(seconds=8)).isoformat(),
                                       rng.choice(attributes.PALETTE) if person else None, rng.choice(attributes.PALETTE) if person else None,
                                       None if person else rng.choice(attributes.PALETTE), None if person else rng.choice(attributes.VEHICLE_TYPES),
                                       rng.choice(attributes.DIRECTIONS), mask))
                        for offset, x in ((0, x0), (8, x1)):
                            detections.append((camera, track, (seen + timedelta(seconds=offset)).isoformat(), "person" if person else "vehicle", x, y - 0.1))
            db.execute("BEGIN")
            db.executemany("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider,upper_colour,lower_colour,vehicle_colour,vehicle_type,direction,zone_mask,detection_count) "
                           "VALUES(?,?,?,?,?,'m','1','CPU',?,?,?,?,?,?,2)", [row + () for row in tracks])
            db.executemany("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                           "VALUES(?,?,?,?,0.9,?,?,0.05,0.2,'m','1','CPU')", detections)
            db.execute("COMMIT")

    @classmethod
    def tearDownClass(cls):
        import os
        for key, value in cls.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.temporary.cleanup()

    def timed(self, **filters) -> tuple[float, list, dict]:
        with connect() as db:
            began = time.perf_counter()
            results, _ = search.find_tracks(db, self.cameras, self.start, self.end, limit=60, **filters)
            names = {camera: camera for camera in self.cameras}
            coverage = search.coverage(db, names, self.start, self.end)
            return time.perf_counter() - began, results, coverage

    def test_searches_over_thirty_days_for_fifty_cameras_answer_within_the_budget(self):
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_tracks").fetchone()[0], self.CAMERAS * self.DAYS * self.TRACKS_PER_CAMERA_DAY)
        timings = {}
        for name, filters in {"no filter": {}, "person, red top, moving right": {"label": "person", "upper_colour": "red", "direction": "right"},
                              "vehicle, truck, white": {"label": "vehicle", "vehicle_type": "truck", "vehicle_colour": "white"},
                              "zone on the left, newest first": {"zone": search.ZONE_PRESETS["left"], "label": "person", "order": "desc"},
                              "rare combination (no match)": {"upper_colour": "pink", "lower_colour": "pink", "direction": "stationary", "zone": search.ZONE_PRESETS["centre"]}}.items():
            elapsed, results, coverage = self.timed(**filters)
            timings[name] = elapsed
            self.assertLess(elapsed, self.BUDGET_SECONDS, f"{name}: {elapsed:.2f}s")
            self.assertEqual(coverage["searched_cameras"], self.CAMERAS)
        print("\nAttribute search timings, 30 days x 50 cameras (%d tracks): %s" % (self.CAMERAS * self.DAYS * self.TRACKS_PER_CAMERA_DAY,
                                                                              ", ".join(f"{name} {seconds * 1000:.0f} ms" for name, seconds in timings.items())))

    def test_the_http_search_is_within_the_budget_too(self):
        from app.application import create_app
        from app.principal import administrator
        app = create_app(start_workers=False)
        app.dependency_overrides[administrator] = lambda: None
        with TestClient(app) as client:
            from app.principal import current_user
            from app.access import Access
            from types import SimpleNamespace
            app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1, role="administrator", token_id=None, access=Access(), username="x", scopes=frozenset({"read"}))
            began = time.perf_counter()
            response = client.get("/api/ai/search", params={"start": self.start.isoformat(), "end": self.end.isoformat(), "label": "person", "upper_colour": "red"})
            elapsed = time.perf_counter() - began
        self.assertEqual(response.status_code, 200, response.text)
        self.assertLess(elapsed, self.BUDGET_SECONDS, f"{elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
