from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from app.retention import acquire_playback, playback_is_leased, release_playback, unlink_if_unleased, unlink_many_if_unleased

ROOT = Path(__file__).resolve().parents[1]
HOLD_LEASE = (
    "import sys, time; from pathlib import Path; from app.retention import acquire_playback; "
    "acquire_playback(Path(sys.argv[1])); print('leased', flush=True); time.sleep(60)"
)


class MediaLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.media = Path(self.temporary.name) / "segment.mp4"
        self.media.write_bytes(b"footage")

    def _lease_in_another_process(self) -> subprocess.Popen:
        holder = subprocess.Popen([sys.executable, "-c", HOLD_LEASE, str(self.media)], cwd=ROOT, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: holder.poll() is None and (holder.kill(), holder.wait()))
        self.assertEqual(holder.stdout.readline().strip(), "leased")
        return holder

    def test_lease_in_another_process_blocks_cleanup_until_it_ends(self):
        holder = self._lease_in_another_process()
        self.assertTrue(playback_is_leased(self.media))
        self.assertFalse(unlink_if_unleased(self.media))
        self.assertTrue(self.media.exists())

        # A crashed holder never strands footage: the lock goes with the process.
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait()
        self.assertFalse(playback_is_leased(self.media))
        self.assertTrue(unlink_if_unleased(self.media))
        self.assertFalse(self.media.exists())

    def test_leases_in_this_process_are_counted(self):
        acquire_playback(self.media)
        acquire_playback(self.media)
        release_playback(self.media)
        self.assertFalse(unlink_if_unleased(self.media))
        release_playback(self.media)
        self.assertFalse(playback_is_leased(self.media))
        self.assertTrue(unlink_if_unleased(self.media))

    def test_group_deletion_is_all_or_nothing(self):
        other = self.media.with_name("other.mp4")
        other.write_bytes(b"more footage")
        self._lease_in_another_process()
        self.assertFalse(unlink_many_if_unleased([other, self.media]))
        self.assertTrue(other.exists())
        self.assertTrue(self.media.exists())

    def test_missing_files_are_harmless(self):
        missing = self.media.with_name("missing.mp4")
        acquire_playback(missing)
        release_playback(missing)
        release_playback(missing)
        self.assertTrue(unlink_if_unleased(missing))

    def test_a_lease_taken_after_deletion_protects_nothing(self):
        self.assertTrue(unlink_if_unleased(self.media))
        acquire_playback(self.media)
        self.assertFalse(playback_is_leased(self.media))
        release_playback(self.media)
        started = time.monotonic()
        self.assertTrue(unlink_if_unleased(self.media))
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
