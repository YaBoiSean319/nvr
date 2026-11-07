"""Looking for watched faces (#253, part 3): the scanner, keeping a sighting, and the alarm of a ``face`` rule. A stand-in reader and picture source stand in for the models and the camera."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from app import face_models, face_scan, faces, rules
from app.main import app
from app.principal import current_user
from app.store import connect, initialize
from tests.test_faces import StandInReader, face, unit
from tests.test_support import principal

NOW = datetime.now(timezone.utc)
PERSON = {"x": 0.05, "y": 0.1, "width": 0.4, "height": 0.8}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.stored: list[tuple] = []
        self.snapshots = 0
        self.clock = Clock()
        self.watching = [("entry-a", [unit(1, 0, 0)])]
        self.found = [face(0.1, 0.2, 0.3, 0.45, unit(0.95, 0.1, 0))]
        self.fail_next = False

    def scanner(self, **kwargs) -> face_scan.FaceScanner:
        async def snapshot():
            self.snapshots += 1
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("the camera did not answer")
            return np.zeros((480, 640, 3), dtype=np.uint8)

        return face_scan.FaceScanner(StandInReader(self.found), snapshot, lambda: self.watching, lambda *args: self.stored.append(args), clock=self.clock, **kwargs)

    def scan(self, scanner: face_scan.FaceScanner) -> int:
        return asyncio.run(scanner.scan())

    def test_a_face_on_a_followed_person_that_matches_the_list_is_kept_once(self):
        scanner = self.scanner()
        scanner.seen("t1", "person", PERSON)
        self.assertEqual(self.scan(scanner), 1)
        track, entry, similarity, kept_face, picture, taken = self.stored[0]
        self.assertEqual((track, entry), ("t1", "entry-a"))
        self.assertGreater(similarity, 0.9)
        self.assertEqual(picture.shape, (480, 640, 3))
        self.clock.now += 10
        self.assertEqual(self.scan(scanner), 0)  # Matched: that person is not looked at again
        self.assertEqual(len(self.stored), 1)

    def test_a_face_that_matches_nobody_is_dropped_and_the_person_is_given_a_few_attempts(self):
        self.found = [face(0.1, 0.2, 0.3, 0.45, unit(0, 0, 1))]
        scanner = self.scanner(max_attempts=3)
        scanner.seen("t1", "person", PERSON)
        for _ in range(5):
            self.clock.now += 10
            self.scan(scanner)
        self.assertEqual((self.stored, self.snapshots), ([], 3))  # Nothing kept; three pictures, then no more for this person

    def test_a_face_not_on_a_followed_person_is_ignored_and_only_people_are_followed(self):
        scanner = self.scanner()
        scanner.seen("car", "vehicle", PERSON)
        self.assertFalse(scanner.due())
        scanner.seen("far", "person", {"x": 0.7, "y": 0.1, "width": 0.2, "height": 0.8})  # Not where the face is
        self.assertEqual(self.scan(scanner), 0)
        self.assertEqual(self.stored, [])

    def test_with_nobody_on_the_list_no_picture_is_taken(self):
        self.watching = []
        scanner = self.scanner()
        scanner.seen("t1", "person", PERSON)
        self.assertEqual(self.scan(scanner), 0)
        self.assertEqual(self.snapshots, 0)

    def test_attempts_wait_for_the_interval_and_a_failed_picture_costs_only_the_attempt(self):
        scanner = self.scanner(interval=3.0)
        scanner.seen("t1", "person", PERSON)
        self.fail_next = True
        self.assertEqual(self.scan(scanner), 0)  # Failed, quietly
        self.assertFalse(scanner.due())  # Too soon
        self.clock.now += 3.5
        self.assertTrue(scanner.due())
        self.assertEqual(self.scan(scanner), 1)

    def test_people_that_are_gone_are_forgotten_and_the_nearest_person_wins(self):
        scanner = self.scanner()
        scanner.seen("t1", "person", PERSON)
        scanner.seen("t2", "person", {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0})
        self.assertEqual(face_scan.associate((0.1, 0.2, 0.3, 0.45), list(scanner.people.items())), "t1")  # The smaller box that holds the face
        scanner.forget({"t2"})
        self.assertEqual(set(scanner.people), {"t2"})


class SightingCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.directory.name) / "state.sqlite3")
        initialize()
        self.camera = "cam-1"
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,X'01')", (self.camera, "Front gate"))
            for track in ("t1", "t2"):
                db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,?,?,?,'m','1','cpu')",
                           (track, self.camera, "person", NOW.isoformat(), NOW.isoformat()))
            self.entry = faces.create_entry(db, "Visitor A", "Trespassed", None, None, 1)
            self.other = faces.create_entry(db, "Visitor B", "Banned", None, None, 1)
            for entry in (self.entry, self.other):
                db.execute("INSERT INTO face_embeddings(entry_id,embedding) VALUES(?,?)", (entry, np.asarray(unit(1, 0, 0) if entry == self.entry else unit(0, 1, 0), dtype=np.float32).tobytes()))
        app.dependency_overrides[current_user] = lambda: principal(1, "admin", "administrator")
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.face = face(0.1, 0.2, 0.3, 0.45, unit(1, 0, 0))
        self.frame = np.full((480, 640, 3), 90, dtype=np.uint8)

    def tearDown(self):
        self.context.__exit__(None, None, None)
        app.dependency_overrides.pop(current_user, None)
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.directory.cleanup()

    def rule(self, **extra) -> dict:
        body = {"name": "Watched face", "camera_ids": [self.camera], "condition": {"type": "face"}, "classes": [], "cooldown_seconds": 0, "actions": [], **extra}
        response = self.client.post("/api/rules", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def keep(self, track="t1", entry=None, at=None, similarity=0.9) -> bool:
        return face_scan.keep_sighting(self.camera, track, entry or self.entry, similarity, self.face, self.frame, at or datetime.now(timezone.utc))

    def count(self, table: str) -> int:
        with connect() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class KeepSightingTests(SightingCase):
    def test_a_match_is_kept_with_a_picture_and_raises_the_alarm_of_a_face_rule(self):
        rule = self.rule()
        self.assertEqual(rule["condition"], {"type": "face", "entry_ids": []})
        self.assertEqual(rule["classes"], [])
        self.assertTrue(self.keep())
        with connect() as db:
            sighting = db.execute("SELECT * FROM face_sightings").fetchone()
            event = db.execute("SELECT condition_type,label,track_id,camera_id FROM rule_events").fetchone()
            alarm = db.execute("SELECT COUNT(*) FROM alarms").fetchone()[0]
        self.assertEqual((sighting["entry_id"], sighting["camera_id"], sighting["track_id"], sighting["similarity"]), (self.entry, self.camera, "t1", 0.9))
        self.assertEqual(bytes(sighting["picture"])[:2], b"\xff\xd8")
        box = json.loads(sighting["box_json"])
        for key, value in {"x": 0.1, "y": 0.2, "width": 0.2, "height": 0.25}.items():
            self.assertAlmostEqual(box[key], value, places=3)
        self.assertEqual((tuple(event), alarm), (("face", "face", "t1", self.camera), 1))

    def test_a_rule_for_particular_people_ignores_the_others(self):
        self.rule(condition={"type": "face", "entry_ids": [self.entry]})
        self.assertTrue(self.keep(track="t1", entry=self.other))  # Still a sighting...
        self.assertEqual((self.count("face_sightings"), self.count("rule_events")), (1, 0))  # ...but not what this rule asks about
        self.assertTrue(self.keep(track="t2", entry=self.entry))
        self.assertEqual(self.count("rule_events"), 1)

    def test_the_cooldown_holds_back_a_second_alarm_on_the_camera(self):
        self.rule(cooldown_seconds=300)
        self.keep(track="t1")
        self.keep(track="t2")
        self.assertEqual((self.count("face_sightings"), self.count("rule_events")), (2, 1))

    def test_no_rule_still_keeps_the_sighting_and_raises_nothing(self):
        self.assertTrue(self.keep())
        self.assertEqual((self.count("face_sightings"), self.count("alarms")), (1, 0))

    def test_a_face_in_a_privacy_mask_is_not_kept(self):
        with connect() as db:
            db.execute("INSERT INTO privacy_masks(camera_id,zones_json,updated_at) VALUES(?,?,?)", (self.camera, json.dumps([{"x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5}]), NOW.isoformat()))
        self.rule()
        self.assertFalse(self.keep())
        self.assertEqual((self.count("face_sightings"), self.count("rule_events")), (0, 0))

    def test_an_expired_or_switched_off_entry_or_a_missing_track_is_not_kept(self):
        self.rule()
        with connect() as db:
            db.execute("UPDATE face_watch_entries SET expires_at=? WHERE id=?", ((NOW - timedelta(days=1)).isoformat(), self.entry))
            db.execute("UPDATE face_watch_entries SET enabled=0 WHERE id=?", (self.other,))
        self.assertFalse(self.keep(entry=self.entry))
        self.assertFalse(self.keep(entry=self.other))
        self.assertFalse(self.keep(entry="gone"))
        with connect() as db:
            db.execute("UPDATE face_watch_entries SET expires_at=NULL WHERE id=?", (self.entry,))
        self.assertFalse(self.keep(track="never-seen"))
        self.assertEqual(self.count("face_sightings"), 0)
        self.assertTrue(self.keep(track="t1", entry=self.entry))

    def test_sightings_go_with_their_entry_and_their_track(self):
        self.keep(track="t1")
        self.keep(track="t2")
        with connect() as db:
            db.execute("DELETE FROM ai_tracks WHERE id='t1'")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM face_sightings").fetchone()[0], 1)
            db.execute("DELETE FROM face_watch_entries WHERE id=?", (self.entry,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM face_sightings").fetchone()[0], 0)


class ScanningTests(SightingCase):
    def test_a_camera_looks_for_faces_only_when_switched_on_and_someone_is_on_the_list(self):
        with connect() as db:
            self.assertEqual(face_scan.scanning(db, self.camera), (False, 0.45))  # Off by default
            db.execute("INSERT INTO face_camera_settings(camera_id,enabled) VALUES(?,1)", (self.camera,))
            self.assertEqual(face_scan.scanning(db, self.camera)[0], True)
            db.execute("UPDATE face_watch_entries SET enabled=0")
            self.assertEqual(face_scan.scanning(db, self.camera)[0], False)  # Nobody to look for
            db.execute("UPDATE face_watch_entries SET enabled=1")
            db.execute("UPDATE face_settings SET match_threshold=0.6")
            self.assertEqual(face_scan.scanning(db, self.camera), (True, 0.6))
        self.assertEqual({entry for entry, _ in face_scan.watch_list()}, {self.entry, self.other})

    def test_a_scan_end_to_end_keeps_the_match_and_nothing_else(self):
        self.rule()
        matching, stranger = face(0.1, 0.2, 0.3, 0.45, unit(1, 0.05, 0)), face(0.6, 0.2, 0.7, 0.4, unit(0, 0, 1))

        async def snapshot():
            return self.frame

        scanner = face_scan.FaceScanner(StandInReader([matching, stranger]), snapshot, face_scan.watch_list,
                                        lambda track, entry, similarity, kept, picture, taken: face_scan.keep_sighting(self.camera, track, entry, similarity, kept, picture, taken))
        scanner.seen("t1", "person", PERSON)
        scanner.seen("t2", "person", {"x": 0.55, "y": 0.1, "width": 0.25, "height": 0.8})
        self.assertEqual(asyncio.run(scanner.scan()), 1)
        with connect() as db:
            self.assertEqual([(row["track_id"], row["entry_id"]) for row in db.execute("SELECT track_id,entry_id FROM face_sightings")], [("t1", self.entry)])
        self.assertEqual(self.count("alarms"), 1)  # The stranger left no trace


class ReplayTests(SightingCase):
    def test_test_mode_replays_the_sightings_a_rule_would_have_fired_on(self):
        created = self.rule(condition={"type": "face", "entry_ids": [self.entry]})
        self.keep(track="t1", entry=self.entry)
        self.keep(track="t2", entry=self.other)
        with connect() as db:
            rule = next(item for item in rules.load_rules(db) if item.id == created["id"])
        events = rules.replay(rule, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
        self.assertEqual([(event["track_id"], event["label"], event["camera_name"]) for event in events], [("t1", "face", "Front gate")])
        self.assertEqual(rules.replay(rule, NOW + timedelta(hours=2), NOW + timedelta(hours=3)), [])


if __name__ == "__main__":
    unittest.main()
