"""Licence plates (#142): reading, searching by full and partial plate, watch lists as rule conditions, access, privacy and deletion with footage."""
from __future__ import annotations

import asyncio
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import numpy as np

from app import plates
from app.store import connect
from app.tracks import Tracker, purge_expired_detections, record_detections
from tests.test_access_grants import GrantCase

NOW = datetime.now(timezone.utc).replace(microsecond=0)
SYMBOLS = plates.DEFAULT_CHARSET + ("<blank>",)
BLANK = len(plates.DEFAULT_CHARSET)


class TextTests(unittest.TestCase):
    def test_plates_are_compared_in_a_normal_form(self):
        self.assertEqual(plates.normalise(" ab-12 cde "), "AB12CDE")
        self.assertEqual(plates.normalise("a" * 40), "A" * plates.MAX_PLATE_LENGTH)
        self.assertEqual(plates.fold("BO1S5"), "80155")
        self.assertEqual(plates.fold("AB*?"), "A8*?")

    def test_a_partial_plate_is_found_inside_and_a_pattern_is_anchored(self):
        self.assertEqual(plates.search_glob("12c"), "*12C*")
        self.assertEqual(plates.search_glob("AB12*"), "AB12*")
        self.assertEqual(plates.search_glob("ab ?? cde"), "AB??CDE")
        self.assertEqual(plates.search_glob("a**b"), "A*B")
        self.assertIsNone(plates.search_glob(" -- "))
        self.assertTrue(plates.matches("AB??CDE", "AB12CDE"))
        self.assertFalse(plates.matches("AB??CDE", "AB123CDE"))
        self.assertTrue(plates.matches("A*E", "ABCDE"))
        self.assertFalse(plates.matches("ABC", "ABCD"))  # A watch list entry is a whole plate
        self.assertTrue(plates.matches("AB12", "A81Z", lookalikes=True))
        self.assertFalse(plates.matches("AB12", "A81Z"))

    def test_a_recogniser_output_is_decoded_as_scores_or_as_indices(self):
        positions = [10, 10, BLANK, 11, BLANK, 1, 1]  # A A _ B _ 1 1 -> AB1 (repeats collapse, a blank separates real repeats)
        scores = np.zeros((len(positions), 1, len(SYMBOLS)), dtype=np.float32)
        for index, symbol in enumerate(positions):
            scores[index, 0, symbol] = 0.9
            scores[index, 0, :] += 0.1 / len(SYMBOLS)
        text, confidence = plates.decode_plate(scores, SYMBOLS, BLANK)
        self.assertEqual(text, "AB1")
        self.assertGreater(confidence, 0.85)
        logits = np.log(scores + 1e-6)  # Not probabilities: they are normalised
        self.assertEqual(plates.decode_plate(logits, SYMBOLS, BLANK)[0], "AB1")
        indices = np.array([[[[10]], [[11]], [[1]], [[-1]], [[3]]]], dtype=np.float32)  # -1 ends the text
        self.assertEqual(plates.decode_plate(indices, SYMBOLS, BLANK), ("AB1", None))
        with self.assertRaises(ValueError):
            plates.decode_plate(np.zeros((5, 3), dtype=np.float32) + 0.5, SYMBOLS, BLANK)

    def test_a_plate_belongs_to_the_smallest_vehicle_it_is_on(self):
        van = {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.5}
        car = {"x": 0.3, "y": 0.4, "width": 0.2, "height": 0.2}
        plate = {"x": 0.35, "y": 0.52, "width": 0.08, "height": 0.03}
        self.assertEqual(plates.associate(plate, [("van", van), ("car", car)]), "car")
        self.assertEqual(plates.associate(plate, [("van", van)]), "van")
        self.assertIsNone(plates.associate({"x": 0.9, "y": 0.9, "width": 0.05, "height": 0.02}, [("van", van), ("car", car)]))
        self.assertEqual(plates.associate({"x": 0.62, "y": 0.45, "width": 0.05, "height": 0.02}, [("van", van)]), "van")  # Just past the box: the margin

    def test_accuracy_is_scored_by_plate_character_and_look_alike(self):
        report = plates.score([("AB12CDE", "AB12CDE"), ("XY99ZZZ", "XY9QZZZ"), ("LM1OP2", "LM10P2"), ("QQ11QQ", None)])
        self.assertEqual((report["plates"], report["read"], report["unread"], report["exact"]), (4, 3, 1, 1))
        self.assertEqual(report["exact_percent"], 25.0)
        self.assertEqual(report["lookalike_tolerant"], 2)  # Q/0 and O/0 are forgiven, not 9/Q
        self.assertEqual(report["character_percent"], round(100 * (7 + 6 + 5 + 0) / 26, 1))


class FakeCore:
    """Stands in for OpenVINO: a detector that finds the plates it was given and a recogniser that reads them in order."""

    def __init__(self, found: list[tuple[float, float, float, float]], texts: list[str]):
        self.found, self.texts = found, list(texts)

    def read_model(self, path: str) -> str:
        return path

    def compile_model(self, model: str, provider: str) -> Any:
        core = self

        class Model:
            def input(self, index: int) -> Any:
                return type("Port", (), {"shape": [1, 3, 24, 94]})()

            def __call__(self, tensor: np.ndarray) -> dict[str, np.ndarray]:
                if "detection" in model:
                    rows = [[0, 2, 0.9, *box] for box in core.found] + [[0, 1, 0.99, 0.0, 0.0, 1.0, 1.0], [0, 2, 0.2, 0.0, 0.0, 0.1, 0.1]]
                    return {"detection_out": np.array(rows, dtype=np.float32).reshape(1, 1, -1, 7)}
                text = core.texts.pop(0)
                indices = [plates.DEFAULT_CHARSET.index(character) for character in text] + [-1]
                return {"decode": np.array(indices, dtype=np.float32).reshape(1, -1, 1, 1)}

        return Model()


class ReaderTests(unittest.TestCase):
    def test_the_reader_finds_plates_and_reads_each_crop(self):
        core = FakeCore([(0.1, 0.5, 0.3, 0.6), (0.6, 0.2, 0.9, 0.3)], ["AB12CDE", "X"])
        reader = plates.PlateReader(Path("/m/vehicle-license-plate-detection-barrier-0106.xml"), Path("/m/recogniser.xml"), core=core)
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        found = reader.read(frame)
        self.assertEqual([plate.text for plate in found], ["AB12CDE"])  # The second read is a single character, which is no plate
        self.assertAlmostEqual(found[0].box["x"], 0.1, places=3)
        self.assertEqual(found[0].crop.shape, (72, 256, 3))  # 0.1 high by 0.2 wide of the frame
        self.assertEqual(found[0].confidence, 0.9)  # The index form carries none of its own: the detector's stands

    def test_a_recogniser_that_also_takes_a_sequence_indicator_is_given_one(self):
        seen = {}

        class TwoInputs(FakeCore):
            def compile_model(self, model, provider):
                inner = super().compile_model(model, provider)
                if "detection" in model:
                    return inner

                class Wrapped:
                    inputs = [type("P", (), {"shape": [1, 3, 24, 94]})(), type("P", (), {"shape": [88, 1]})()]

                    def input(self, index):
                        return inner.input(index)

                    def __call__(self, feed):
                        seen["feed"] = feed
                        return inner(feed[0])

                return Wrapped()

        reader = plates.PlateReader(Path("/m/vehicle-license-plate-detection-barrier-0106.xml"), Path("/m/recogniser.xml"), core=TwoInputs([(0.1, 0.5, 0.3, 0.6)], ["AB12CDE"]))
        self.assertEqual([plate.text for plate in reader.read(np.zeros((720, 1280, 3), dtype=np.uint8))], ["AB12CDE"])
        indicator = seen["feed"][1]
        self.assertEqual((indicator.shape, indicator[0, 0], indicator[1, 0]), ((88, 1), 0.0, 1.0))

    def test_a_charset_file_beside_the_recogniser_replaces_the_default(self):
        with TemporaryDirectory() as directory:
            model = Path(directory) / "recogniser.xml"
            (Path(directory) / "recogniser.charset.txt").write_text("<blank>\nA\nB\n", encoding="utf-8")
            self.assertEqual(plates.read_charset(model), (("<blank>", "A", "B"), 0))
        self.assertEqual(plates.read_charset(Path("/nowhere/recogniser.xml"))[1], BLANK)


class StandInReader:
    """A reader that returns the plates it is told to, on whatever frame it is given."""

    reader_id = "stand-in"

    def __init__(self, reads: list[plates.Plate]):
        self.reads = reads
        self.calls = 0

    def read(self, frame: np.ndarray) -> list[plates.Plate]:
        self.calls += 1
        return list(self.reads)


VEHICLE_BOX = {"x": 0.3, "y": 0.35, "width": 0.3, "height": 0.35}
PLATE_BOX = {"x": 0.4, "y": 0.6, "width": 0.1, "height": 0.04}


def plate(text: str, confidence: float = 0.9, box: dict[str, float] | None = None) -> plates.Plate:
    crop = np.full((20, 60, 3), 200, dtype=np.uint8)
    return plates.Plate(text, confidence, box or PLATE_BOX, crop)


class PlateCase(GrantCase):
    def vehicle(self, camera_id: str, at: datetime | None = None, box: dict[str, float] | None = None) -> str:
        """A stored vehicle track, as the AI worker leaves one."""
        with connect() as db:
            return record_detections(db, camera_id, [{"label": "vehicle", "confidence": 0.9, "box": box or VEHICLE_BOX}], at or NOW, Tracker(),
                                     model_id="m", model_version="1", provider="CPU")[0]

    def read(self, camera_id: str, text: str, *, at: datetime | None = None, confidence: float = 0.9, track: str | None = None) -> str:
        at = at or datetime.now(timezone.utc)
        track = track or self.vehicle(camera_id, at)
        self.assertTrue(plates.keep_plate(camera_id, track, plate(text, confidence), at, "stand-in"))
        return track

    def search(self, query: str, client=None, **params) -> list[dict]:
        response = (client or self.admin).get("/api/plates/search", params={"q": query, **params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["results"]

    def watch_list(self, kind: str, *patterns: str, name: str | None = None, lookalikes: bool = False) -> str:
        response = self.admin.post("/api/admin/plates/watch-lists", json={"name": name or f"{kind} list", "kind": kind, "lookalikes": lookalikes})
        self.assertEqual(response.status_code, 201, response.text)
        for pattern in patterns:
            added = self.admin.post(f"/api/admin/plates/watch-lists/{response.json()['id']}/entries", json={"pattern": pattern})
            self.assertEqual(added.status_code, 201, added.text)
        return response.json()["id"]

    def rule(self, list_id: str, cameras: list[str] | None = None, **extra) -> str:
        response = self.admin.post("/api/rules", json={"name": "Gate plates", "camera_ids": cameras or [self.d1], "condition": {"type": "plate", "watch_list_id": list_id}, **extra})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def alarms(self) -> list[dict]:
        with connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM alarms ORDER BY created_at,id")]


class ScannerTests(PlateCase):
    def test_a_vehicle_gets_its_plate_read_and_stored_with_its_track_confidence_and_crop(self):
        track = self.vehicle(self.d1)
        reader = StandInReader([plate("AB12CDE", 0.93), plate("ZZ99ZZZ", 0.95, {"x": 0.8, "y": 0.9, "width": 0.1, "height": 0.04}), plate("LOW111", 0.3)])
        stored: list[tuple[str, plates.Plate]] = []

        async def snapshot():
            return np.zeros((720, 1280, 3), dtype=np.uint8)

        scanner = plates.PlateScanner(reader, snapshot, lambda track_id, found: (stored.append((track_id, found)), plates.keep_plate(self.d1, track_id, found, NOW, "stand-in")),
                                      min_confidence=0.6, interval=0.0)
        scanner.seen(track, "vehicle", VEHICLE_BOX)
        scanner.seen("a-person", "person", VEHICLE_BOX)  # Only vehicles are looked at
        self.assertEqual(asyncio.run(scanner.scan()), 1)
        self.assertEqual([(track_id, found.text) for track_id, found in stored], [(track, "AB12CDE")])  # Off every vehicle, or below the camera's confidence: not kept
        row = self.search("AB12")[0]
        self.assertEqual((row["track_id"], row["camera_id"], row["confidence"], row["has_crop"]), (track, self.d1, 0.93, True))
        crop = self.admin.get(f"/api/plates/reads/{row['id']}/crop")
        self.assertEqual((crop.status_code, crop.headers["content-type"], crop.content[:2]), (200, "image/jpeg", b"\xff\xd8"))
        self.assertEqual(crop.headers["cache-control"], "private, no-store")
        # A vehicle with a read is not looked at again
        self.assertEqual(asyncio.run(scanner.scan()), 0)
        self.assertEqual(reader.calls, 1)

    def test_a_vehicle_without_a_read_is_tried_a_few_times_and_then_left(self):
        track = self.vehicle(self.d1)
        reader = StandInReader([])
        clock = [0.0]

        async def snapshot():
            return np.zeros((720, 1280, 3), dtype=np.uint8)

        scanner = plates.PlateScanner(reader, snapshot, lambda *_: None, interval=3.0, max_attempts=3, clock=lambda: clock[0])
        scanner.seen(track, "vehicle", VEHICLE_BOX)
        for _ in range(6):
            asyncio.run(scanner.scan())
            asyncio.run(scanner.scan())  # Too soon after the last: no second snapshot
            clock[0] += 3.0
        self.assertEqual(reader.calls, 3)
        scanner.forget(set())
        self.assertEqual(scanner.vehicles, {})

    def test_a_weak_read_is_tried_again_and_the_better_read_is_kept(self):
        track = self.vehicle(self.d1)
        reads = [[plate("AB12CDE", 0.7)], [plate("AB12CDE", 0.95)], [plate("AB12CDE", 0.99)]]

        class Sequence:
            def read(self, frame):
                return reads.pop(0)

        async def snapshot():
            return np.zeros((720, 1280, 3), dtype=np.uint8)

        scanner = plates.PlateScanner(Sequence(), snapshot, lambda track_id, found: plates.keep_plate(self.d1, track_id, found, NOW, "stand-in"), interval=0.0)
        scanner.seen(track, "vehicle", VEHICLE_BOX)
        for _ in range(3):
            asyncio.run(scanner.scan())
        self.assertEqual(len(reads), 1)  # The 95% read ended the attempts
        self.assertEqual([item["confidence"] for item in self.search("AB12CDE")], [0.95])
        self.assertAlmostEqual(datetime.fromisoformat(self.search("AB12CDE")[0]["read_at"]).timestamp(), datetime.now(timezone.utc).timestamp(), delta=60)  # Stamped when the picture was taken

    def test_a_failed_snapshot_or_reader_costs_an_attempt_and_nothing_else(self):
        track = self.vehicle(self.d1)

        class Broken:
            def read(self, frame):
                raise RuntimeError("the model failed")

        async def snapshot():
            return np.zeros((720, 1280, 3), dtype=np.uint8)

        async def none():
            return None

        for reader, source in ((Broken(), snapshot), (StandInReader([plate("AB12CDE")]), none)):
            scanner = plates.PlateScanner(reader, source, lambda *_: self.fail("nothing should be stored"), interval=0.0)
            scanner.seen(track, "vehicle", VEHICLE_BOX)
            with self.assertLogs("nvr.plates", level="ERROR") if isinstance(reader, Broken) else self.assertNoLogs("nvr.plates"):
                self.assertEqual(asyncio.run(scanner.scan()), 0)
            self.assertFalse(scanner.running)

    def test_a_plate_in_a_masked_area_is_never_kept(self):
        track = self.vehicle(self.d1)
        with connect() as db:
            db.execute("INSERT INTO privacy_masks(camera_id,zones_json,camera_side,updated_at) VALUES(?,?,'none',?)", (self.d1, json.dumps([{"x": 0.35, "y": 0.55, "width": 0.3, "height": 0.2}]), NOW.isoformat()))
        self.assertFalse(plates.keep_plate(self.d1, track, plate("AB12CDE"), NOW, "stand-in"))
        self.assertEqual(self.search("AB12"), [])

    def test_a_read_for_a_track_that_went_with_its_footage_is_dropped(self):
        self.assertFalse(plates.keep_plate(self.d1, str(uuid.uuid4()), plate("AB12CDE"), NOW, "stand-in"))
        self.assertEqual(self.search("AB12"), [])

    def test_the_most_confident_read_of_a_plate_on_a_track_is_the_one_kept(self):
        track = self.read(self.d1, "AB12CDE", confidence=0.7)
        self.read(self.d1, "AB12CDE", confidence=0.95, track=track)
        self.read(self.d1, "AB12CDE", confidence=0.8, track=track)
        results = self.search("AB12CDE")
        self.assertEqual([(item["plate"], item["confidence"]) for item in results], [("AB12CDE", 0.95)])
        self.read(self.d1, "AB12CD", confidence=0.6, track=track)  # A different reading of the same car is another read
        self.assertEqual(len(self.search("AB12CD")), 2)


class SearchTests(PlateCase):
    def setUp(self):
        super().setUp()
        for camera, text, minutes in ((self.d1, "AB12CDE", 1), (self.d2, "AB99XYZ", 2), (self.d1, "KT55QRS", 3), (self.y1, "AB12CDE", 4), (self.d1, "O0I1S5B8", 5)):
            self.read(camera, text, at=NOW - timedelta(minutes=10 - minutes))

    def plates(self, query: str, client=None, **params) -> list[str]:
        return [item["plate"] for item in self.search(query, client, **params)]

    def test_a_partial_plate_a_prefix_a_suffix_and_a_pattern_all_find_plates(self):
        self.assertEqual(self.plates("AB12CDE"), ["AB12CDE", "AB12CDE"])
        self.assertEqual(self.plates("12cd"), ["AB12CDE", "AB12CDE"])  # Inside the plate
        self.assertEqual(sorted(self.plates("AB*")), ["AB12CDE", "AB12CDE", "AB99XYZ"])
        self.assertEqual(self.plates("*QRS"), ["KT55QRS"])
        self.assertEqual(self.plates("AB??CDE"), ["AB12CDE", "AB12CDE"])
        self.assertEqual(self.plates("AB?CDE"), [])  # Anchored: one character short
        self.assertEqual(self.plates("zzz"), [])
        self.assertEqual(self.admin.get("/api/plates/search", params={"q": "  --  "}).status_code, 422)

    def test_look_alike_characters_are_forgiven_unless_asked_not_to(self):
        self.assertEqual(self.plates("AB1ZCDE"), ["AB12CDE", "AB12CDE"])  # Z for 2
        self.assertEqual(self.plates("AB1ZCDE", lookalikes="false"), [])
        self.assertEqual(self.plates("OOLLS5B8"), ["O0I1S5B8"])
        self.assertEqual(self.plates("0011558B"), ["O0I1S5B8"])

    def test_results_are_newest_first_on_chosen_cameras_in_a_range_and_above_a_confidence(self):
        results = self.search("AB12CDE")
        self.assertEqual([item["camera_id"] for item in results], [self.y1, self.d1])
        self.assertEqual([item["camera_id"] for item in self.search("AB12CDE", order="asc")], [self.d1, self.y1])
        self.assertEqual([item["camera_id"] for item in self.search("AB12CDE", camera_ids=[self.d1])], [self.d1])
        self.assertEqual(len(self.search("AB*", start=(NOW - timedelta(minutes=8)).isoformat(), end=NOW.isoformat())), 2)
        self.assertEqual(len(self.search("AB*", limit=1)), 1)
        self.assertTrue(self.admin.get("/api/plates/search", params={"q": "AB*", "limit": 1}).json()["truncated"])
        self.assertEqual(self.search("AB12CDE", min_confidence=0.95), [])
        self.assertEqual(self.admin.get("/api/plates/search", params={"q": "AB", "start": "2020-01-01T00:00:00Z", "end": NOW.isoformat()}).status_code, 422)  # Over 92 days
        self.assertEqual(self.admin.get("/api/plates/search", params={"q": "AB", "start": "yesterday"}).status_code, 422)

    def test_a_person_sees_only_plates_on_cameras_they_may_see(self):
        self.assertEqual(sorted(item["camera_id"] for item in self.search("AB*", self.limited)), sorted([self.d1, self.d2]))
        self.assertEqual(self.search("AB12CDE", self.limited, camera_ids=[self.y1]), [])  # Asking for a hidden camera finds nothing and says nothing
        hidden = next(item for item in self.search("AB12CDE") if item["camera_id"] == self.y1)
        self.assertEqual(self.limited.get(f"/api/plates/reads/{hidden['id']}/crop").status_code, 404)
        self.assertEqual(self.limited.get(f"/api/plates/reads/{uuid.uuid4()}/crop").status_code, 404)
        self.assertEqual(self.full.get(f"/api/plates/reads/{hidden['id']}/crop").status_code, 200)

    def test_searching_is_a_read_and_needs_no_administrator(self):
        self.assertEqual(self.limited.get("/api/admin/plates").status_code, 403)
        self.assertEqual(self.limited.put(f"/api/admin/plates/cameras/{self.d1}", json={"enabled": True}).status_code, 403)
        self.assertEqual(self.limited.post("/api/admin/plates/watch-lists", json={"name": "x", "kind": "deny"}).status_code, 403)


class WatchListTests(PlateCase):
    def alarm_labels(self) -> list[str]:
        return [alarm["label"] for alarm in self.alarms()]

    def test_a_deny_list_plate_raises_the_rule_and_an_allow_list_plate_does_not(self):
        deny = self.watch_list("deny", "AB12CDE", "KT*", name="Banned")
        allow = self.watch_list("allow", "OK11OKK", name="Residents")
        self.rule(deny)
        self.admin.post("/api/rules", json={"name": "Strangers", "camera_ids": [self.d1], "condition": {"type": "plate", "watch_list_id": allow}})
        self.read(self.d1, "AB12CDE")  # On the deny list: raises it (and is not a resident)
        self.read(self.d1, "XX00XXX")  # On neither: raises the allow-list rule only
        self.read(self.d1, "OK11OKK")  # On the allow list: raises nothing at all
        self.read(self.d1, "KT5555")  # Matches the deny pattern KT*
        # A deny list raises its plates; an allow list raises every plate that is not on it, and nothing for the one that is
        self.assertEqual(sorted(alarm["rule_name"] + ":" + alarm["label"] for alarm in self.alarms()),
                         ["Gate plates:plate:AB12CDE", "Gate plates:plate:KT5555", "Strangers:plate:AB12CDE", "Strangers:plate:KT5555", "Strangers:plate:XX00XXX"])
        self.assertTrue(all(alarm["condition_type"] == "plate" and alarm["track_id"] for alarm in self.alarms()))

    def test_a_rule_watches_only_its_cameras_and_does_not_fire_twice_for_a_track_or_inside_its_cooldown(self):
        deny = self.watch_list("deny", "AB12CDE")
        self.rule(deny, cooldown_seconds=600)
        self.read(self.y1, "AB12CDE")  # Another camera
        self.assertEqual(self.alarms(), [])
        track = self.read(self.d1, "AB12CDE")
        self.read(self.d1, "AB12CDE", track=track, confidence=0.99)  # The same plate on the same track
        self.read(self.d1, "AB12CDE", at=datetime.now(timezone.utc) + timedelta(seconds=30))  # Another vehicle inside the cooldown
        self.read(self.d1, "AB12CDE", at=datetime.now(timezone.utc) + timedelta(seconds=900))  # Outside it
        self.assertEqual(len(self.alarms()), 2)

    def test_an_expired_entry_matches_nothing_and_look_alikes_follow_the_lists_setting(self):
        deny = self.watch_list("deny", name="Banned")
        entry = self.admin.post(f"/api/admin/plates/watch-lists/{deny}/entries", json={"pattern": "AB12CDE", "expires_at": (NOW + timedelta(hours=1)).isoformat()})
        self.assertEqual(entry.status_code, 201, entry.text)
        self.rule(deny)
        self.read(self.d1, "AB12CDE")  # Raised: the entry is live
        with connect() as db:
            db.execute("UPDATE plate_watch_entries SET expires_at=?", ((NOW - timedelta(minutes=1)).isoformat(),))
        self.read(self.d1, "AB12CDE", at=datetime.now(timezone.utc) + timedelta(minutes=5))
        self.assertEqual(len(self.alarms()), 1)
        view = self.admin.get("/api/admin/plates").json()["watch_lists"][0]
        self.assertTrue(view["entries"][0]["expired"])
        self.read(self.d1, "AB1ZCDE", at=datetime.now(timezone.utc) + timedelta(minutes=10))  # Z for 2: not on the list as written
        self.assertEqual(len(self.alarms()), 1)
        with connect() as db:
            db.execute("UPDATE plate_watch_entries SET expires_at=NULL")
        self.assertEqual(self.admin.put(f"/api/admin/plates/watch-lists/{deny}", json={"name": "Banned", "kind": "deny", "lookalikes": True}).status_code, 200)
        self.read(self.d1, "AB1ZCDE", at=datetime.now(timezone.utc) + timedelta(minutes=20))
        self.assertEqual(len(self.alarms()), 2)

    def test_the_rule_test_replays_the_reads_kept(self):
        deny = self.watch_list("deny", "AB12CDE")
        self.read(self.d1, "AB12CDE", at=NOW - timedelta(hours=2))
        self.read(self.d1, "ZZ00ZZZ", at=NOW - timedelta(hours=1))
        response = self.admin.post("/api/rules/test", json={"name": "t", "camera_ids": [self.d1], "condition": {"type": "plate", "watch_list_id": deny}})
        self.assertEqual(response.status_code, 200, response.text)
        events = response.json()["events"]
        self.assertEqual([event["label"] for event in events], ["plate:AB12CDE"])
        self.assertEqual(self.alarms(), [])  # Test mode writes nothing

    def test_lists_and_entries_are_managed_by_administrators_with_checks(self):
        deny = self.watch_list("deny", "AB12CDE", name="Banned")
        self.assertEqual(self.admin.post("/api/admin/plates/watch-lists", json={"name": "banned", "kind": "allow"}).status_code, 409)  # Names are unique, ignoring case
        self.assertEqual(self.admin.post(f"/api/admin/plates/watch-lists/{deny}/entries", json={"pattern": "ab-12 cde"}).status_code, 409)  # The same plate
        self.assertEqual(self.admin.post(f"/api/admin/plates/watch-lists/{deny}/entries", json={"pattern": "*"}).status_code, 422)  # Would match everything
        self.assertEqual(self.admin.post(f"/api/admin/plates/watch-lists/{deny}/entries", json={"pattern": "A*"}).status_code, 422)
        self.assertEqual(self.admin.post(f"/api/admin/plates/watch-lists/{deny}/entries", json={"pattern": "AB1", "expires_at": "2020-01-01T00:00:00Z"}).status_code, 422)
        self.assertEqual(self.admin.post(f"/api/admin/plates/watch-lists/{deny}/entries", json={"pattern": "AB1", "expires_at": "2099-01-01T00:00:00"}).status_code, 422)  # No time zone
        self.assertEqual(self.admin.post("/api/admin/plates/watch-lists/nope/entries", json={"pattern": "AB12"}).status_code, 404)
        stored = self.admin.get("/api/admin/plates").json()["watch_lists"][0]
        self.assertEqual((stored["name"], stored["kind"], [entry["pattern"] for entry in stored["entries"]]), ("Banned", "deny", ["AB12CDE"]))
        self.assertEqual(self.admin.delete(f"/api/admin/plates/watch-lists/{deny}/entries/{stored['entries'][0]['id']}").status_code, 204)
        self.assertEqual(self.admin.delete(f"/api/admin/plates/watch-lists/{deny}/entries/{stored['entries'][0]['id']}").status_code, 404)
        self.assertTrue({"watch_list_created", "watch_list_entry_added", "watch_list_entry_removed"} <= {row["action"] for row in self.audit_all()})

    def audit_all(self) -> list[dict]:
        with connect() as db:
            return [dict(row) for row in db.execute("SELECT action FROM audit_log")]

    def test_a_list_used_by_a_rule_cannot_be_deleted_or_change_kind_and_a_rule_needs_a_real_list(self):
        deny = self.watch_list("deny", "AB12CDE")
        self.rule(deny)
        self.assertEqual(self.admin.delete(f"/api/admin/plates/watch-lists/{deny}").status_code, 409)
        self.assertEqual(self.admin.put(f"/api/admin/plates/watch-lists/{deny}", json={"name": "deny list", "kind": "allow"}).status_code, 409)
        missing = self.admin.post("/api/rules", json={"name": "x", "camera_ids": [self.d1], "condition": {"type": "plate", "watch_list_id": "nope"}})
        self.assertEqual(missing.status_code, 422)
        unused = self.watch_list("allow", name="Unused")
        self.assertEqual(self.admin.delete(f"/api/admin/plates/watch-lists/{unused}").status_code, 204)

    def test_cameras_are_designated_in_the_settings_and_the_models_are_reported(self):
        state = self.admin.get("/api/admin/plates").json()
        self.assertFalse(state["models"]["available"])
        self.assertEqual({camera["id"]: camera["enabled"] for camera in state["cameras"]}[self.d1], False)
        self.assertEqual(self.admin.put(f"/api/admin/plates/cameras/{self.d1}", json={"enabled": True, "min_confidence": 0.75}).status_code, 200)
        with connect() as db:
            self.assertEqual(plates.plate_settings(db, self.d1), (True, 0.75))
            self.assertEqual(plates.plate_settings(db, self.d2), (False, 0.6))
        self.assertEqual(self.admin.put(f"/api/admin/plates/cameras/{uuid.uuid4()}", json={"enabled": True}).status_code, 404)
        self.assertEqual(self.admin.put(f"/api/admin/plates/cameras/{self.d1}", json={"enabled": True, "min_confidence": 2}).status_code, 422)
        self.assertEqual(self.search("AB", camera_ids=[self.d1]), [])
        self.assertEqual(self.admin.get("/api/plates/search", params={"q": "AB"}).json()["cameras_reading"], [self.d1])


class DeletionTests(PlateCase):
    def test_reads_are_deleted_with_their_footage(self):
        old = NOW - timedelta(days=40)
        first, second = self.add_segment(self.d1, 0), self.add_segment(self.d1, 1)
        with connect() as db:
            starts = dict(db.execute("SELECT id,starts_at FROM recording_segments WHERE camera_id=?", (self.d1,)).fetchall())
        first_start = datetime.fromisoformat(starts[first])
        second_start = datetime.fromisoformat(starts[second])
        self.read(self.d1, "AB12CDE", at=first_start + timedelta(seconds=10))
        self.read(self.d1, "KT55QRS", at=second_start + timedelta(seconds=10))
        self.read(self.d2, "ZZ00ZZZ", at=old)  # No footage at all for this camera
        with connect() as db:
            db.execute("DELETE FROM recording_segments WHERE id=?", (first,))
        self.assertEqual(self.search("AB12CDE", start=(first_start - timedelta(days=1)).isoformat(), end=(first_start + timedelta(days=1)).isoformat()), [])
        self.assertEqual(len(self.search("KT55QRS", start=(second_start - timedelta(days=1)).isoformat(), end=(second_start + timedelta(days=1)).isoformat())), 1)

    def test_reads_older_than_retention_that_no_footage_covers_expire_with_their_detections(self):
        track = self.read(self.d1, "AB12CDE", at=NOW - timedelta(days=40))
        self.read(self.d1, "KT55QRS", at=NOW - timedelta(days=1))
        with connect() as db:
            purge_expired_detections(db, self.d1, NOW - timedelta(days=30))
            self.assertIsNone(db.execute("SELECT 1 FROM plate_reads WHERE track_id=?", (track,)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM ai_tracks WHERE id=?", (track,)).fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM plate_reads").fetchone()[0], 1)

    def test_reads_go_with_their_track_and_their_camera(self):
        track = self.read(self.d1, "AB12CDE")
        self.read(self.d2, "KT55QRS")
        with connect() as db:
            db.execute("DELETE FROM ai_detections WHERE track_id=?", (track,))  # The track goes with its last detection, and the read with the track
            self.assertEqual(db.execute("SELECT COUNT(*) FROM plate_reads").fetchone()[0], 1)
            db.execute("DELETE FROM cameras WHERE id=?", (self.d2,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM plate_reads").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
