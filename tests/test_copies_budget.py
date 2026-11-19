"""One disk budget for exports, case packages and playback copies, so making them cannot push recordings out (#362).

The owner's decision: together they may use 10% of the recordings volume. At the limit, playback copies (which can be made again) are
removed first, oldest first; then a new export or case package is refused with the reason. Exports are kept 7 days by default, at most 30."""
from __future__ import annotations

import os
import shutil
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from app import copies_budget, exports, privacy, storage
from app.store import connect
from tests.test_access_grants import START, GrantCase

MIB = 1 << 20


class BudgetCase(GrantCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.environment["NVR_RECORDINGS_PATH"])
        self.copies = self.root / privacy.MASKED_COPY_DIRECTORY
        self.copies.mkdir(parents=True, exist_ok=True)

    def budget(self, size: int):
        return patch.object(copies_budget, "budget_bytes", return_value=size)

    def file(self, path: Path, size: int, age_seconds: float = 0) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            handle.truncate(size)
        if age_seconds:
            moment = time.time() - age_seconds
            os.utime(path, (moment, moment))
        return path


class BudgetTests(BudgetCase):
    def test_usage_is_counted_for_each_of_the_three(self):
        self.file(self.root / exports.EXPORT_DIRECTORY / "a.zip", 3 * MIB)
        self.file(self.root / "cases" / "k" / "p.zip", 2 * MIB)
        self.file(self.copies / "x-y.mp4", 1 * MIB)
        self.assertEqual(copies_budget.usage(), {"exports": 3 * MIB, "case_packages": 2 * MIB, "playback_copies": 1 * MIB})

    def test_playback_copies_make_way_oldest_first_and_then_a_new_export_is_refused(self):
        old = self.file(self.copies / "old.mp4", 4 * MIB, age_seconds=600)
        new = self.file(self.copies / "new.mp4", 4 * MIB, age_seconds=60)
        self.file(self.root / exports.EXPORT_DIRECTORY / "kept.zip", 4 * MIB)
        with self.budget(13 * MIB):
            copies_budget.make_room(2 * MIB)  # 12 used + 2 is over 13: fits once the oldest copy is gone
            self.assertEqual((old.exists(), new.exists()), (False, True))
            with self.assertRaises(copies_budget.BudgetFull) as refused:
                copies_budget.make_room(20 * MIB)
        self.assertFalse(new.exists())  # Every copy went before the refusal
        self.assertTrue((self.root / exports.EXPORT_DIRECTORY / "kept.zip").exists())  # Nobody's export is removed to make room
        self.assertIn("share of the recordings disk", str(refused.exception))

    def test_a_playback_copy_is_never_refused_but_older_copies_make_way(self):
        old = self.file(self.copies / "old.mp4", 4 * MIB, age_seconds=600)
        self.file(self.root / exports.EXPORT_DIRECTORY / "kept.zip", 8 * MIB)
        with self.budget(10 * MIB):
            copies_budget.make_room(4 * MIB, refuse=False)
        self.assertFalse(old.exists())

    def test_an_export_is_refused_up_front_with_the_reason(self):
        self.add_segment(self.d1)
        self.file(self.root / exports.EXPORT_DIRECTORY / "kept.zip", 8 * MIB)
        with self.budget(8 * MIB):
            refused = self.admin.post("/api/exports", json={"camera_ids": [self.d1], "output_modes": ["individual"],
                                                           "start": START.isoformat(), "end": (START + timedelta(seconds=30)).isoformat()})
        self.assertEqual(refused.status_code, 507, refused.text)
        self.assertIn("share of the recordings disk", refused.json()["detail"])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM exports").fetchone()[0], 0)


class SweepTests(BudgetCase):
    def test_copies_unused_for_a_day_are_swept_on_a_timer(self):
        stale = self.file(self.copies / "stale.mp4", MIB, age_seconds=2 * 86400)
        fresh = self.file(self.copies / "fresh.mp4", MIB, age_seconds=60)
        self.assertEqual(privacy.sweep_copies(), 1)
        self.assertEqual((stale.exists(), fresh.exists()), (False, True))

    def test_a_copy_goes_with_the_footage_it_was_made_from(self):
        segment = self.root / f"{self.d1}/0.mp4"
        self.file(segment, MIB)
        other = self.root / f"{self.d1}/1.mp4"
        self.file(other, MIB)
        made_from_it = self.file(self.copies / f"{privacy._source_key(segment)}-aaaa.mp4", MIB)
        made_from_other = self.file(self.copies / f"{privacy._source_key(other)}-bbbb.mp4", MIB)
        segment_id = self.add_segment(self.d1)
        with connect() as db:
            row = db.execute("SELECT * FROM recording_segments WHERE id=?", (segment_id,)).fetchone()
        self.assertTrue(storage.remove_segment_files([row]))
        self.assertEqual((segment.exists(), made_from_it.exists(), made_from_other.exists()), (False, False, True))


class ExportSettingsTests(BudgetCase):
    def test_exports_are_kept_seven_days_unless_an_administrator_chooses_up_to_thirty(self):
        shown = self.admin.get("/api/export-storage").json()
        self.assertEqual((shown["retention_days"], shown["max_retention_days"], shown["share"]), (7, 30, 0.1))
        self.assertEqual(set(shown["used_bytes"]), {"exports", "case_packages", "playback_copies"})
        self.assertEqual(self.admin.put("/api/export-storage", json={"retention_days": 30}).json()["retention_days"], 30)
        for bad in (0, 31):
            self.assertEqual(self.admin.put("/api/export-storage", json={"retention_days": bad}).status_code, 422, bad)
        self.assertEqual(self.limited.get("/api/export-storage").status_code, 403)
        self.assertEqual(self.limited.put("/api/export-storage", json={"retention_days": 1}).status_code, 403)
        with connect() as db:
            self.assertEqual(exports.retention_days(db), 30)
            audited = db.execute("SELECT details_json FROM audit_log WHERE action='export_retention_changed'").fetchone()[0]
        self.assertEqual(audited, '{"after": 30, "before": 7}')

    def test_an_export_leaves_the_volumes_recording_headroom_free(self):
        with connect() as db:
            db.execute("INSERT INTO storage_headroom(volume_id,headroom_bytes) VALUES('default',?)", (10 << 30,))
        disk = shutil._ntuple_diskusage(total=100 << 30, used=92 << 30, free=8 << 30)
        with patch("app.exports.shutil.disk_usage", return_value=disk):
            self.assertEqual(exports.free_floor(self.root), 10 << 30)
            with self.assertRaises(RuntimeError) as refused:
                exports._ensure_export_space(self.root)
        self.assertIn("headroom", str(refused.exception))


if __name__ == "__main__":
    unittest.main()
