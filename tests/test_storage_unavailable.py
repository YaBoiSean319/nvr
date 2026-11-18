"""Storage that cannot be reached is not treated as footage already deleted (#338).

When an archive disk is not mounted, "no file found" used to count as "removed": retention and the headroom pass deleted the index rows while the
files stayed on the absent disk, so the footage vanished from the timeline for good. The rows now stay until the storage is back."""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

from app import headroom, tiering
from app.retention import cleanup_expired_recordings
from app.storage import StorageUnavailable, remove_segment_files
from app.store import connect
from tests.test_headroom import NOW, UNIT, HeadroomCase


class UnmountedVolumeTests(HeadroomCase):
    def setUp(self):
        super().setUp()
        for volume in ("default", "archive"):
            headroom.set_headroom(volume, 0)
        self.old = [self.segment("north", hours_ago=24 * 400, volume="archive") for _ in range(3)]  # Far past any retention policy

    def unmounted(self):
        """The archive folder is there but its disk is not mounted."""
        return patch("app.storage.os.path.ismount", return_value=False)

    def files(self) -> int:
        return sum(1 for _ in (self.root / "archive").rglob("*.mp4"))

    def test_retention_keeps_the_rows_of_footage_on_a_volume_that_is_not_mounted(self):
        with self.unmounted(), self.assertLogs("nvr.retention", level="WARNING") as logged:
            result = cleanup_expired_recordings(NOW)
        self.assertEqual((result["removed_segments"], result["waiting_for_storage"]), (0, 3))
        self.assertEqual(self.remaining(), self.old)
        self.assertEqual(self.files(), 3)
        self.assertIn("storage is not available", logged.output[0])

    def test_when_the_volume_is_back_the_expired_footage_is_removed_as_usual(self):
        with self.unmounted():
            cleanup_expired_recordings(NOW)
        result = cleanup_expired_recordings(NOW)
        self.assertEqual((result["removed_segments"], result["waiting_for_storage"]), (3, 0))
        self.assertEqual((self.remaining(), self.files()), ([], 0))

    def test_the_headroom_pass_leaves_a_volume_that_is_not_mounted_alone(self):
        headroom.set_headroom("archive", 900)  # Far more than is free: mounted, this removes the oldest footage
        with self.unmounted():
            result = headroom.keep_headroom(NOW)
        self.assertNotIn("archive", result)
        self.assertEqual(self.remaining(), self.old)
        self.assertEqual(headroom.keep_headroom(NOW)["archive"]["removed_segments"], 2)  # Mounted again, the pass does its work

    def test_removing_files_says_the_storage_is_unavailable_instead_of_finding_nothing(self):
        with connect() as db:
            row = db.execute("SELECT id,relative_path,tier_id,tier_key,tier_sha256 FROM recording_segments WHERE id=?", (self.old[0],)).fetchone()
        with self.unmounted(), self.assertRaises(StorageUnavailable):
            remove_segment_files([row])
        self.assertTrue(remove_segment_files([row]))


class AbsentLocalTierTests(HeadroomCase):
    """Footage that has moved to a local cold tier whose folder is missing or empty (its disk is not mounted)."""

    def setUp(self):
        super().setUp()
        self.tier_root = Path(self.temporary.name) / "cold"
        self.tier_root.mkdir()
        self.tier_id = str(uuid.uuid4())
        segment = self.segment("north", hours_ago=24 * 400)
        with connect() as db:
            relative = db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (segment,)).fetchone()[0]
            self.row = {"id": segment, "relative_path": relative, "tier_id": self.tier_id, "tier_key": relative, "tier_sha256": "0" * 64}
        (self.root / relative).unlink()  # It moved: the hot file is gone
        self.cold = self.tier_root / relative
        self.cold.parent.mkdir(parents=True)
        self.cold.write_bytes(b"x" * UNIT)

    def local_tier(self):
        tier = tiering.Tier(self.tier_id, "Cold", "local", str(self.tier_root), None, None, "", "", None, None)
        return patch.object(tiering, "load_tier", lambda db, tier_id: tier)

    def test_a_cold_file_that_is_there_is_found(self):
        with self.local_tier():
            self.assertEqual(tiering.local_files(self.row, None), [self.cold.resolve()])

    def test_an_empty_or_missing_tier_folder_is_unavailable_not_empty_of_footage(self):
        self.cold.unlink()
        self.cold.parent.rmdir()
        for parent in list(self.tier_root.iterdir()):
            parent.rmdir()
        with self.local_tier(), self.assertRaises(StorageUnavailable):
            tiering.local_files(self.row, None)
        self.tier_root.rmdir()
        with self.local_tier(), self.assertRaises(StorageUnavailable):
            tiering.local_files(self.row, None)

    def test_one_file_missing_from_a_tier_that_is_there_is_just_missing(self):
        self.cold.unlink()
        (self.tier_root / "another-camera").mkdir()
        with self.local_tier():
            self.assertEqual(tiering.local_files(self.row, None), [])
