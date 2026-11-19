"""Deleting an archived camera's footage works in batches and always closes its files (#361).

It did everything in one write transaction, with every file of the camera open and locked at once: more files than the open-file limit
failed with all of them left open, and a camera with a month of footage held the write lock for most of an hour."""
from __future__ import annotations

import errno
import os
import resource
from datetime import datetime, timezone
from unittest import mock

from app import retention
from app.api import footage
from app.store import connect
from tests import test_camera_archive



def open_files() -> int:
    return len(os.listdir("/proc/self/fd"))


class FootageDeleteCase(test_camera_archive.CameraArchiveTests):
    def many_segments(self, count: int) -> list:
        folder = self.recordings / self.camera_id
        folder.mkdir(parents=True, exist_ok=True)
        rows = []
        for index in range(count):
            (folder / f"s{index:05d}.mp4").write_bytes(b"x")
            rows.append((self.camera_id, f"{self.camera_id}/s{index:05d}.mp4", f"2026-10-21T10:{index // 60 % 60:02d}:{index % 60:02d}+00:00", f"2026-10-21T11:00:{index % 60:02d}+00:00", 1))
        with connect() as db:
            db.executemany("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)", rows)
        self.assertEqual(self.client.delete(f"/api/cameras/{self.camera_id}").status_code, 204)  # Archived
        return sorted(folder.iterdir())

    def delete(self):
        return self.client.request("DELETE", f"/api/cameras/{self.camera_id}/footage", json={"confirm_camera_name": "Archive camera"})

    def left(self) -> tuple[int, int]:
        """(segment rows, files) the camera still has."""
        with connect() as db:
            rows = db.execute("SELECT COUNT(*) FROM recording_segments WHERE camera_id=?", (self.camera_id,)).fetchone()[0]
        return rows, len(list((self.recordings / self.camera_id).iterdir()))


# The parent's own tests are not repeated here.
for name in [name for name in dir(test_camera_archive.CameraArchiveTests) if name.startswith("test_")]:
    setattr(FootageDeleteCase, name, None)


class BatchTests(FootageDeleteCase):
    def test_a_camera_with_more_files_than_the_open_file_limit_is_deleted(self):
        self.many_segments(700)
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (open_files() + 400, hard))  # Room for a batch, not for 700 files at once
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        before = open_files()
        deleted = self.delete()
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertEqual(self.left(), (0, 0))
        self.assertLessEqual(open_files(), before + 2)
        with connect() as db:
            audit = db.execute("SELECT details_json FROM audit_log WHERE action='delete_camera_footage'").fetchone()[0]
        self.assertIn('"segments_deleted":700', audit)

    def test_the_write_lock_is_released_between_batches(self):
        self.many_segments(footage.FOOTAGE_DELETE_BATCH * 3 + 10)
        sizes: list[int] = []
        real = footage.connect

        def watched():
            db = real()
            original = db.execute

            class Watching:
                def __getattr__(self, name):
                    return getattr(db, name)

                def __enter__(self):
                    db.__enter__()
                    return self

                def __exit__(self, *details):
                    return db.__exit__(*details)

                def execute(self, sql, *args):
                    if sql.startswith("DELETE FROM recording_segments"):
                        sizes.append(len(args[0]))
                    return original(sql, *args)
            return Watching()

        with mock.patch.object(footage, "connect", watched):
            self.assertEqual(self.delete().status_code, 204)
        self.assertEqual(sizes, [footage.FOOTAGE_DELETE_BATCH] * 3 + [10])  # One short transaction a batch, not one for the camera
        self.assertEqual(self.left(), (0, 0))

    def test_its_detections_go_too_in_batches(self):
        self.many_segments(3)
        with connect() as db:
            for index in range(footage.FOOTAGE_DELETE_BATCH + 5):
                db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,'person','2026-10-21T10:00:00Z','2026-10-21T10:00:01Z','m','1','CPU')",
                           (f"track-{index}", self.camera_id))
        self.assertEqual(self.delete().status_code, 204)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_tracks WHERE camera_id=?", (self.camera_id,)).fetchone()[0], 0)


class FailureTests(FootageDeleteCase):
    def test_a_file_that_cannot_be_opened_part_way_leaves_no_file_open_and_the_delete_can_be_run_again(self):
        files = self.many_segments(footage.FOOTAGE_DELETE_BATCH + 50)
        broken = files[footage.FOOTAGE_DELETE_BATCH + 20].resolve()  # In the second batch
        real_open, before, failing = os.open, open_files(), [False]

        def sometimes(path, flags, *args, **kwargs):
            if failing[0] and os.fspath(path) == str(broken):
                raise OSError(errno.EIO, "Input/output error")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(retention.os, "open", sometimes):
            failing[0] = False
            # The check before anything is removed opens each file once; let that pass, and fail when the batch opens them together.
            real_locks = retention._exclusive_locks

            def locks(paths):
                failing[0] = len(paths) > 1
                try:
                    return real_locks(paths)
                finally:
                    failing[0] = False

            with mock.patch.object(retention, "_exclusive_locks", locks):
                refused = self.delete()
        self.assertEqual(refused.status_code, 503, refused.text)
        self.assertEqual(open_files(), before, "every file opened for the delete was closed again")
        self.assertEqual(self.left(), (50, 50))  # The first batch went whole; nothing of the second did, rows or files
        again = self.delete()
        self.assertEqual(again.status_code, 204, again.text)
        self.assertEqual(self.left(), (0, 0))

    def test_rows_whose_files_are_already_gone_are_cleared_by_running_it_again(self):
        files = self.many_segments(30)
        for path in files[:10]:
            path.unlink()  # As if the server stopped between removing a batch's files and its rows
        self.assertEqual(self.delete().status_code, 204)
        self.assertEqual(self.left(), (0, 0))

    def test_a_file_being_played_stops_the_delete_before_anything_is_removed(self):
        files = self.many_segments(footage.FOOTAGE_DELETE_BATCH + 5)
        retention.acquire_playback(files[-1])
        try:
            refused = self.delete()
        finally:
            retention.release_playback(files[-1])
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(self.left(), (footage.FOOTAGE_DELETE_BATCH + 5,) * 2)

    def test_a_hold_or_an_export_stops_it_before_anything_is_removed(self):
        from app.exports import create_export_record
        files = self.many_segments(footage.FOOTAGE_DELETE_BATCH + 5)
        export_id = create_export_record(1, [self.camera_id], datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc), datetime(2026, 10, 21, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(self.delete().status_code, 409)
        self.assertEqual(self.left(), (len(files), len(files)))
        with connect() as db:
            db.execute("UPDATE exports SET status='failed' WHERE id=?", (export_id,))
        self.assertEqual(self.delete().status_code, 204)


class SingleSegmentTests(FootageDeleteCase):
    def test_a_segment_a_queued_export_will_read_is_not_deleted(self):
        from app.exports import create_export_record
        segment_id, media = self.add_segment()
        export_id = create_export_record(1, [self.camera_id], datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc), datetime(2026, 10, 21, 10, 1, tzinfo=timezone.utc))
        refused = self.client.delete(f"/api/cameras/{self.camera_id}/segments/{segment_id}")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertIn("export", refused.json()["detail"])
        self.assertTrue(media.is_file())
        with connect() as db:
            db.execute("UPDATE exports SET status='completed' WHERE id=?", (export_id,))
        self.assertEqual(self.client.delete(f"/api/cameras/{self.camera_id}/segments/{segment_id}").status_code, 204)
        self.assertFalse(media.exists())
