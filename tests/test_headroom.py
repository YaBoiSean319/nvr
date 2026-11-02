"""Storage headroom (#219): free space is kept by deleting the oldest footage, whatever its retention policy, except what is held, reserved, covered by an alarm or being played."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import headroom
from app.main import app
from app.principal import administrator, current_user
from app.retention import acquire_playback, release_playback
from app.store import connect, initialize

Usage = namedtuple("Usage", "total used free")
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
UNIT = 100


class HeadroomCase(unittest.TestCase):
    CAPACITY = 1000

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.keys = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_STORAGE_VOLUMES")
        self.previous = {key: os.environ.get(key) for key in self.keys}
        self.root = Path(self.temporary.name) / "recordings"
        (self.root / "archive").mkdir(parents=True)
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root)
        os.environ["NVR_STORAGE_VOLUMES"] = json.dumps({"default": "", "archive": "archive"})
        initialize()
        self.cameras = {name: str(uuid.uuid4()) for name in ("north", "south")}
        with connect() as db:
            for name, camera_id in self.cameras.items():
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_enabled) VALUES(?,?,X'01',1)", (camera_id, name))
        self.usage = patch.object(headroom.shutil, "disk_usage", side_effect=self.disk_usage)
        self.usage.start()
        self.ismount = patch("app.storage.os.path.ismount", return_value=True)
        self.ismount.start()

    def tearDown(self):
        self.ismount.stop()
        self.usage.stop()
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def disk_usage(self, path):
        """A 1000-unit disk that holds exactly what the catalogue says is on it: the default volume's footage, or the archive's."""
        archive = Path(path).name == "archive"
        with connect() as db:
            used = db.execute("SELECT COALESCE(SUM(size_bytes),0) FROM recording_segments WHERE (relative_path LIKE 'archive/%')=?", (1 if archive else 0,)).fetchone()[0]
        return Usage(self.CAPACITY, used, self.CAPACITY - used)

    def segment(self, camera: str, hours_ago: int, volume: str = "default") -> int:
        ends = NOW - timedelta(hours=hours_ago)
        relative = f"{'archive/' if volume == 'archive' else ''}{self.cameras[camera]}/{uuid.uuid4()}.mp4"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        with connect() as db:
            return int(db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                                  (self.cameras[camera], relative, (ends - timedelta(minutes=10)).isoformat(), ends.isoformat(), UNIT)).lastrowid)

    def remaining(self) -> list[int]:
        with connect() as db:
            return [row[0] for row in db.execute("SELECT id FROM recording_segments ORDER BY ends_at")]


class KeepHeadroomTests(HeadroomCase):
    def setUp(self):
        super().setUp()
        for volume in ("default", "archive"):  # Each test sets the one it is about; the default (5 GiB) would swallow a 1000-unit disk
            headroom.set_headroom(volume, 0)

    def test_the_oldest_footage_across_cameras_goes_first_until_the_headroom_is_free(self):
        ids = [self.segment("north" if hour % 2 else "south", hour) for hour in range(9, 0, -1)]  # 900 of 1000 used: 100 free; oldest first
        headroom.set_headroom("default", 300)
        result = headroom.keep_headroom(NOW)
        self.assertEqual(result["default"], {"removed_segments": 2, "removed_bytes": 200, "short_bytes": 0})
        self.assertEqual(self.remaining(), ids[2:])  # The two oldest, one from each camera
        self.assertEqual(headroom.keep_headroom(NOW)["default"]["removed_segments"], 0)  # Enough room now: nothing more goes

    def test_footage_inside_its_retention_policy_is_removed_because_storage_comes_first(self):
        self.segment("north", 1)
        for hour in range(8, 1, -1):
            self.segment("south", hour)
        headroom.set_headroom("default", 500)
        with connect() as db:
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,3650)", (self.cameras["south"],))
        headroom.keep_headroom(NOW)
        self.assertEqual(len(self.remaining()), 5)

    def test_held_footage_and_footage_being_played_are_never_removed_and_the_next_oldest_goes(self):
        ids = [self.segment("north", hour) for hour in range(8, 0, -1)]  # 800 used
        held, played = ids[0], ids[1]
        with connect() as db:
            db.execute("INSERT INTO recording_holds(id,segment_id,camera_id) VALUES(?,?,?)", (str(uuid.uuid4()), held, self.cameras["north"]))
            path = self.root / db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (played,)).fetchone()[0]
        acquire_playback(path)
        try:
            headroom.set_headroom("default", 400)
            result = headroom.keep_headroom(NOW)
        finally:
            release_playback(path)
        self.assertEqual(result["default"]["removed_segments"], 2)
        self.assertEqual(self.remaining(), [held, played] + ids[4:])

    def test_footage_reserved_by_an_export_is_kept(self):
        ids = [self.segment("north", hour) for hour in range(8, 0, -1)]
        with connect() as db:
            export = str(uuid.uuid4())
            db.execute("INSERT INTO exports(id,camera_ids_json,requested_start,requested_end,status,created_at,updated_at) VALUES(?,?,?,?,'running',?,?)", (export, "[]", NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), NOW.isoformat()))
            db.execute("INSERT INTO export_sources(export_id,segment_id) VALUES(?,?)", (export, ids[0]))
        headroom.set_headroom("default", 300)
        headroom.keep_headroom(NOW)
        self.assertIn(ids[0], self.remaining())
        self.assertEqual(len(self.remaining()), 7)

    def test_a_long_run_of_held_footage_at_the_front_does_not_stop_newer_footage_being_removed(self):
        ids = [self.segment("north", hour) for hour in range(9, 0, -1)]
        with connect() as db:
            for segment in ids[:6]:
                db.execute("INSERT INTO recording_holds(id,segment_id,camera_id) VALUES(?,?,?)", (str(uuid.uuid4()), segment, self.cameras["north"]))
        headroom.set_headroom("default", 200)
        with patch.object(headroom, "MAX_SEGMENTS_PER_PASS", 2):
            result = headroom.keep_headroom(NOW)
        self.assertEqual(result["default"]["removed_segments"], 1)  # The held six are not among the two it looks at
        self.assertEqual(self.remaining(), ids[:6] + ids[7:])

    def test_a_headroom_larger_than_the_volume_allows_is_capped(self):
        with connect() as db:
            headroom.set_headroom("default", 10**12)
            self.assertEqual(headroom.effective_headroom(db, "default", 1000), 900)

    def test_when_everything_is_protected_the_shortfall_is_reported_instead(self):
        ids = [self.segment("north", hour) for hour in range(9, 0, -1)]
        with connect() as db:
            for segment in ids:
                db.execute("INSERT INTO recording_holds(id,segment_id,camera_id) VALUES(?,?,?)", (str(uuid.uuid4()), segment, self.cameras["north"]))
        headroom.set_headroom("default", 300)
        result = headroom.keep_headroom(NOW)
        self.assertEqual(result["default"], {"removed_segments": 0, "removed_bytes": 0, "short_bytes": 200})
        with connect() as db:
            shown = headroom.status(db, "default", self.CAPACITY, 100, {"default": "", "archive": "archive"}, NOW)
        self.assertEqual((shown["short_bytes"], shown["below_headroom"], shown["removing_oldest_footage"]), (200, True, False))

    def test_a_volume_only_loses_its_own_footage(self):
        default = [self.segment("north", hour) for hour in range(9, 0, -1)]
        archive = [self.segment("south", hour, "archive") for hour in range(9, 0, -1)]
        headroom.set_headroom("archive", 300)
        result = headroom.keep_headroom(NOW)
        self.assertEqual(result["archive"]["removed_segments"], 2)
        self.assertEqual(result["default"]["removed_segments"], 0)
        self.assertEqual([i for i in self.remaining() if i in default], default)
        self.assertEqual(len([i for i in self.remaining() if i in archive]), 7)

    def test_what_was_removed_is_reported_beside_the_setting(self):
        for hour in range(9, 0, -1):
            self.segment("north", hour)
        headroom.set_headroom("default", 300)
        headroom.keep_headroom(NOW)
        with connect() as db:
            shown = headroom.status(db, "default", self.CAPACITY, 300, {"default": "", "archive": "archive"}, NOW + timedelta(minutes=1))
        self.assertEqual((shown["headroom_bytes"], shown["headroom_is_default"]), (300, False))
        self.assertEqual((shown["removed_24h_segments"], shown["removed_24h_bytes"], shown["removing_oldest_footage"]), (2, 200, True))
        self.assertEqual(shown["longest_policy_days"], 30)
        self.assertGreaterEqual(shown["oldest_footage_days"], 0)

    def test_the_default_headroom_is_a_share_of_the_volume_with_a_floor(self):
        self.assertEqual(headroom.default_headroom(1000 * headroom.GIB), 50 * headroom.GIB)
        self.assertEqual(headroom.default_headroom(40 * headroom.GIB), 5 * headroom.GIB)
        self.assertEqual(headroom.default_headroom(None), 5 * headroom.GIB)

    def test_a_segments_volume_is_the_longest_configured_path_that_prefixes_it(self):
        volumes = {"default": "", "archive": "archive", "deep": "archive/deep"}
        self.assertEqual(headroom.volume_of("cam/a.mp4", volumes), "default")
        self.assertEqual(headroom.volume_of("archive/cam/a.mp4", volumes), "archive")
        self.assertEqual(headroom.volume_of("archive/deep/cam/a.mp4", volumes), "deep")
        self.assertEqual(headroom.volume_of("archived/cam/a.mp4", volumes), "default")


class HeadroomApiTests(HeadroomCase):
    def setUp(self):
        super().setUp()
        app.dependency_overrides[current_user] = lambda: {"id": 1, "username": "admin", "role": "administrator"}
        app.dependency_overrides[administrator] = lambda: {"id": 1, "username": "admin", "role": "administrator"}
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        app.dependency_overrides.pop(current_user, None)
        app.dependency_overrides.pop(administrator, None)
        super().tearDown()

    def test_an_administrator_sets_the_headroom_and_sees_the_status_with_each_volume(self):
        with connect() as db:
            db.execute("INSERT OR IGNORE INTO users(id,username,password_hash,role) VALUES(1,'admin','x','administrator')")
        shown = self.client.get("/api/storage").json()["volumes"]
        default = next(volume for volume in shown if volume["id"] == "default")
        self.assertTrue(default["headroom"]["headroom_is_default"])
        saved = self.client.put("/api/storage/default/headroom", json={"headroom_bytes": 500})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["headroom_bytes"], 500)
        again = next(volume for volume in self.client.get("/api/storage").json()["volumes"] if volume["id"] == "default")
        self.assertEqual((again["headroom"]["headroom_bytes"], again["headroom"]["headroom_is_default"]), (500, False))
        reset = self.client.put("/api/storage/default/headroom", json={"headroom_bytes": None})
        self.assertTrue(reset.json()["headroom_is_default"])
        with connect() as db:
            actions = [row[0] for row in db.execute("SELECT action FROM audit_log WHERE action='storage_headroom_changed'")]
        self.assertEqual(len(actions), 2)

    def test_a_headroom_most_of_the_volume_is_refused(self):
        self.assertEqual(self.client.put("/api/storage/default/headroom", json={"headroom_bytes": int(self.CAPACITY * 0.95)}).status_code, 422)

    def test_an_unknown_volume_and_a_negative_headroom_are_refused(self):
        self.assertEqual(self.client.put("/api/storage/nowhere/headroom", json={"headroom_bytes": 1}).status_code, 404)
        self.assertEqual(self.client.put("/api/storage/default/headroom", json={"headroom_bytes": -1}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
