"""An export fetches footage that has moved to a cold tier before it takes the database's write lock (#358).

Finding a cold source's file downloads it. Done under the lock that protects leasing, an hour of cold footage stopped every process writing for
as long as sixty downloads took."""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from app import exports, storage
from app.retention import release_playback
from app.store import connect
from tests.test_headroom import NOW, HeadroomCase


class ColdSourcesTests(HeadroomCase):
    def setUp(self):
        super().setUp()
        self.segments = [self.segment("north", hours_ago=hour) for hour in (3, 2)]
        with connect() as db:
            for segment in self.segments:  # Both have moved to a cold tier
                db.execute("UPDATE recording_segments SET tier_id='cold-1',tier_key=relative_path,tier_sha256='0' WHERE id=?", (segment,))
            self.paths = [row[0] for row in db.execute("SELECT relative_path FROM recording_segments ORDER BY starts_at")]
            db.execute("INSERT INTO exports(id,requested_by,camera_ids_json,requested_start,requested_end,status,created_at,updated_at) VALUES('job-1',NULL,?,?,?,'queued',?,?)",
                       (json.dumps([self.cameras["north"]]), (NOW - timedelta(hours=4)).isoformat(), NOW.isoformat(), NOW.isoformat(), NOW.isoformat()))
        self.fetched: list[tuple[str, bool]] = []
        real = storage.recording_file_path

        def fetching(relative_path: str) -> Path:
            """Stands in for the download: notes whether another connection could write at that moment."""
            if relative_path not in [path for path, _ in self.fetched]:
                self.fetched.append((relative_path, self.can_write()))
            return real(relative_path)

        for module in (storage, exports):
            patcher = patch.object(module, "recording_file_path", fetching)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def can_write() -> bool:
        other = sqlite3.connect(connect().execute("PRAGMA database_list").fetchone()[2], timeout=0.05, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            other.close()

    def test_every_cold_source_is_fetched_while_other_processes_can_still_write(self):
        job = exports._claim_export("job-1")
        acquired: list[Path] = []
        try:
            per_camera, total = exports._reserve_sources(job, acquired)
        finally:
            for path in acquired:
                release_playback(path)
        self.assertEqual(total, 2)
        self.assertEqual(self.fetched, [(path, True) for path in self.paths])
        self.assertEqual(len(acquired), 2)

    def test_only_footage_that_has_moved_is_fetched_ahead(self):
        with connect() as db:
            db.execute("UPDATE recording_segments SET tier_id=NULL,tier_key=NULL,tier_sha256=NULL WHERE id=?", (self.segments[0],))
        storage.fetch_cold_sources([self.cameras["north"]], NOW - timedelta(hours=4), NOW)
        self.assertEqual([path for path, _ in self.fetched], [self.paths[1]])

    def test_a_tier_that_cannot_be_reached_fails_the_export_with_no_lock_held(self):
        def unreachable(relative_path: str) -> Path:
            self.fetched.append((relative_path, self.can_write()))
            raise OSError("The cold tier cannot be reached")

        job = exports._claim_export("job-1")
        with patch.object(storage, "recording_file_path", unreachable), self.assertRaises(OSError):
            exports._reserve_sources(job, [])
        self.assertEqual(self.fetched, [(self.paths[0], True)])
        self.assertTrue(self.can_write(), "the write lock was left held")
