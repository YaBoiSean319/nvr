"""The recorder indexes only the segments FFmpeg finished since its last pass (#334).

FFmpeg's segment list gains a line per finished segment and is never trimmed. Reading all of it every two seconds, with a database connection and
a write lock per line, made a pass cost more the longer a camera stayed connected. These pin the cost: nothing new means no database at all, and
what is new is stored in one transaction."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

from app import recording
from app.recording import RecordingManager
from app.store import connect
from tests.test_access_grants import GrantCase


class SegmentIndexTests(GrantCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.environment["NVR_RECORDINGS_PATH"])
        self.output = self.root / self.d1
        self.output.mkdir(parents=True, exist_ok=True)
        self.list = self.root / "segments.csv"
        self.list.write_text("")
        self.manager = RecordingManager()

    def finish(self, minute: int, terminated: bool = True) -> str:
        """What FFmpeg does when a segment is finished: the file is there and its line is added to the list."""
        name = f"segment-20260102T00{minute:02d}00Z.mp4"
        (self.output / name).write_bytes(b"x" * 500)
        with self.list.open("a") as handle:
            handle.write(f"{name},0.000000,60.000000" + ("\n" if terminated else ""))
        return name

    def index(self) -> tuple[int, int]:
        """(segments newly indexed, database connections the pass opened)."""
        opened = 0
        real = recording.connect

        def counting():
            nonlocal opened
            opened += 1
            return real()

        with mock.patch.object(recording, "connect", counting):
            return self.manager._index_csv(self.d1, self.list, self.output), opened

    def stored(self) -> int:
        with connect() as db:
            return db.execute("SELECT COUNT(*) FROM recording_segments WHERE camera_id=?", (self.d1,)).fetchone()[0]

    def test_a_pass_with_nothing_new_touches_no_database_however_long_the_list_is(self):
        for minute in range(50):
            self.finish(minute)
        self.assertEqual(self.index(), (50, 1))  # Fifty segments, one connection, one transaction
        for _ in range(3):
            self.assertEqual(self.index(), (0, 0))
        self.assertEqual(self.stored(), 50)

    def test_only_the_new_lines_are_stored(self):
        self.finish(0)
        self.assertEqual(self.index(), (1, 1))
        self.finish(1)
        self.finish(2)
        with mock.patch.object(recording.metrics, "segment_written", wraps=recording.metrics.segment_written) as written:
            self.assertEqual(self.index(), (2, 1))
        self.assertEqual(written.call_count, 2)
        self.assertEqual(self.stored(), 3)

    def test_segments_that_could_not_be_stored_are_tried_again(self):
        self.finish(0)
        self.finish(1)

        def busy():
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(recording, "connect", busy):
            self.assertEqual(self.manager._index_csv(self.d1, self.list, self.output), 0)
        self.assertEqual(self.stored(), 0)
        self.assertEqual(self.index(), (2, 1))

    def test_a_line_still_being_written_is_read_again_and_never_lost(self):
        self.finish(0)
        self.finish(1, terminated=False)
        self.assertEqual(self.index()[0], 2)
        with self.list.open("a") as handle:
            handle.write("\n")
        self.finish(2)
        self.assertEqual(self.index()[0], 1)
        self.assertEqual(self.index(), (0, 0))
        self.assertEqual(self.stored(), 3)

    def test_a_new_run_with_a_new_list_starts_from_its_first_line(self):
        self.finish(0)
        self.assertEqual(self.index()[0], 1)
        self.list.write_text("")  # A shorter list than was remembered
        self.finish(5)
        self.assertEqual(self.index()[0], 1)
        self.assertEqual(self.stored(), 2)

    def test_footage_written_is_marked_once_per_pass(self):
        with connect() as db:
            db.execute("UPDATE cameras SET last_footage_write_at=NULL,recording_fault_since='2026-01-01T00:00:00.000Z' WHERE id=?", (self.d1,))
        self.finish(0)
        self.assertEqual(self.index(), (1, 1))
        with connect() as db:
            row = db.execute("SELECT last_footage_write_at,recording_fault_since FROM cameras WHERE id=?", (self.d1,)).fetchone()
        self.assertIsNotNone(row["last_footage_write_at"])
        self.assertIsNone(row["recording_fault_since"])
