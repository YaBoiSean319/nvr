"""The signed-footage chain's costs do not grow with a camera's age (#352).

Three did: asking "is there anything new to sign?" read every segment ever recorded; verifying any range loaded and re-hashed the whole
chain; and an export's chain file ran from its first segment to the newest signature, a month of links for a minute of old footage."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import footage_chain
from app.store import connect
from tests.test_access_grants import GrantCase

T0 = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
ROUND = 3  # Segments recorded between one signing and the next


class ChainCase(GrantCase):
    def setUp(self):
        super().setUp()
        footage_chain._unreadable.clear()
        footage_chain._looked.clear()
        self.addCleanup(footage_chain._looked.clear)
        self.folder = Path(self.environment["NVR_RECORDINGS_PATH"]) / self.d1
        self.folder.mkdir(parents=True, exist_ok=True)
        self.count = 0

    def record(self, segments: int) -> list[int]:
        """Index that many more one-minute segments, each with a small file of its own."""
        ids = []
        with connect() as db:
            for _ in range(segments):
                name = f"s{self.count:06d}.mp4"
                (self.folder / name).write_bytes(f"footage {self.count}".encode())
                starts = T0 + timedelta(minutes=self.count)
                ids.append(db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                                      (self.d1, f"{self.d1}/{name}", starts.isoformat(), (starts + timedelta(minutes=1)).isoformat(), 10)).lastrowid)
                self.count += 1
        return ids

    def chain(self) -> None:
        while asyncio.run(footage_chain.append_pending()):
            pass
        footage_chain.sign_heads()

    def history(self, rounds: int) -> list[int]:
        """A camera that has recorded and been signed that many times. Returns the first round's segment ids."""
        first = self.record(ROUND)
        self.chain()
        for _ in range(rounds - 1):
            self.record(ROUND)
            self.chain()
        return first

    def steps(self, work) -> int:
        counted = [0]
        with connect() as db:
            db.set_progress_handler(lambda: counted.__setitem__(0, counted[0] + 1) or 0, 20)
            work(db)
            db.set_progress_handler(None, 0)
        return counted[0]


class PendingCheckTests(ChainCase):
    def test_asking_whether_anything_is_new_costs_the_same_however_much_was_recorded(self):
        def cost(rounds: int) -> int:
            self.history(rounds)
            self.assertEqual(asyncio.run(footage_chain.append_pending()), 0)  # Looked at once since the last segment: the mark is set
            return self.steps(lambda db: self.assertEqual(footage_chain._unchained(db, footage_chain.BATCH), []))

        small, large = cost(5), cost(400)
        self.assertLess(large, small * 2 + 20, f"{large} steps after 1,200 more segments, {small} before")

    def test_a_pass_with_nothing_new_takes_no_write_lock(self):
        self.history(3)
        seen: list[str] = []
        real = footage_chain.connect

        def watched():
            db = real()
            db.set_trace_callback(seen.append)
            return db

        from unittest import mock
        with mock.patch.object(footage_chain, "connect", watched):
            self.assertEqual(asyncio.run(footage_chain.append_pending()), 0)
            self.assertEqual(footage_chain.sign_heads(), 0)
        self.assertFalse([statement for statement in seen if statement.upper().startswith(("BEGIN", "INSERT", "UPDATE", "DELETE"))], seen)

    def test_new_footage_is_still_found_at_once_and_older_footage_on_the_whole_look(self):
        self.history(3)
        new = self.record(1)
        self.assertEqual(asyncio.run(footage_chain.append_pending()), 1)
        self.assertEqual(asyncio.run(footage_chain.append_pending()), 0)  # A look that finds nothing moves the mark to the newest segment
        with connect() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM footage_chain WHERE segment_id=?", (new[0],)).fetchone())
            # A segment below the mark that lost its link (as one whose storage came back would have none)
            db.execute("DELETE FROM footage_chain WHERE segment_id=?", (new[0],))
        self.assertEqual(asyncio.run(footage_chain.append_pending()), 0)  # Not looked at on an ordinary pass
        for database, (mark, _) in list(footage_chain._looked.items()):
            footage_chain._looked[database] = (mark, time.monotonic() - footage_chain.FULL_SCAN_SECONDS - 1)  # The whole look is due
        self.assertEqual(asyncio.run(footage_chain.append_pending()), 1)


class ExtractTests(ChainCase):
    def test_the_chain_file_for_old_footage_holds_its_own_stretch_not_everything_since(self):
        first = self.history(200)
        with connect() as db:
            extract = footage_chain.extract(db, self.d1, first[:1])
            sources = [{"segment_id": row["segment_id"], "relative_path": row["relative_path"], "sha256": row["sha256"]}
                       for row in db.execute("SELECT segment_id,relative_path,sha256 FROM footage_chain WHERE segment_id=?", (first[0],))]
            total = db.execute("SELECT COUNT(*) FROM footage_chain").fetchone()[0]
        self.assertEqual(total, 200 * ROUND)
        self.assertLessEqual(len(extract["links"]), ROUND, "the extract ran on past the first signature that covers the footage")
        checked = footage_chain.verify_extract(extract, sources)
        self.assertEqual((checked["valid"], checked["unsigned"]), (True, []))

    def test_a_package_made_before_this_change_still_verifies(self):
        first = self.history(30)
        with connect() as db:  # As it was built: from the first segment's link to the newest signature
            latest = footage_chain._latest_signature(db, self.d1)
            start = db.execute("SELECT MIN(seq) FROM footage_chain WHERE segment_id=?", (first[0],)).fetchone()[0]
            links = [dict(row) for row in db.execute("SELECT camera_id,seq,kind,segment_id,relative_path,starts_at,ends_at,size_bytes,sha256,prev_hash,link_hash "
                                                     "FROM footage_chain WHERE camera_id=? AND seq BETWEEN ? AND ? ORDER BY seq", (self.d1, start, latest["upto_seq"]))]
            old = {"camera_id": self.d1, "links": links, "signature": {key: latest[key] for key in ("upto_seq", "head_hash", "signature", "public_key", "signed_at")}}
            sources = [{"segment_id": first[0], "relative_path": links[0]["relative_path"], "sha256": links[0]["sha256"]}]
        self.assertEqual(len(links), 30 * ROUND)
        self.assertTrue(footage_chain.verify_extract(old, sources)["valid"])

    def test_footage_not_signed_yet_is_past_the_end_and_reported_as_unsigned(self):
        self.history(3)
        fresh = self.record(1)
        asyncio.run(footage_chain.append_pending())  # Hashed, not signed yet
        with connect() as db:
            older = db.execute("SELECT segment_id,relative_path,sha256 FROM footage_chain WHERE seq=1").fetchone()
            newer = db.execute("SELECT segment_id,relative_path,sha256 FROM footage_chain WHERE segment_id=?", (fresh[0],)).fetchone()
            extract = footage_chain.extract(db, self.d1, [older["segment_id"], fresh[0]])
        checked = footage_chain.verify_extract(extract, [dict(older), dict(newer)])
        self.assertEqual((checked["valid"], checked["unsigned"]), (True, [fresh[0]]))


class VerifyRangeTests(ChainCase):
    def check(self, minutes_from: int = 0, minutes_to: int = ROUND) -> dict:
        return footage_chain.verify(self.d1, T0 + timedelta(minutes=minutes_from), T0 + timedelta(minutes=minutes_to))

    def test_verifying_an_old_hour_hashes_its_own_stretch_of_the_chain(self):
        self.history(200)
        hashed = [0]
        real = footage_chain.link_hash

        def counting(link):
            hashed[0] += 1
            return real(link)

        from unittest import mock
        with mock.patch.object(footage_chain, "link_hash", counting):
            result = self.check()
        self.assertTrue(result["valid"], result)
        self.assertEqual([entry["status"] for entry in result["segments"]], ["ok"] * ROUND)
        self.assertLessEqual(hashed[0], ROUND * 4, f"{hashed[0]} links were re-hashed to verify {ROUND} segments of a {200 * ROUND}-link chain")

    def test_a_link_changed_in_the_range_is_still_caught_wherever_the_range_is(self):
        self.history(40)
        middle = 20 * ROUND
        with connect() as db:
            db.execute("UPDATE footage_chain SET sha256=? WHERE seq=?", ("0" * 64, middle + 1))
        result = self.check(middle, middle + ROUND)
        self.assertFalse(result["valid"])
        self.assertIn("changed after it was written", result["chain_error"])

    def test_a_link_removed_from_the_range_or_a_signature_rewritten_is_caught(self):
        self.history(40)
        middle = 20 * ROUND
        with connect() as db:
            db.execute("UPDATE footage_chain_signatures SET head_hash=? WHERE upto_seq=?", ("f" * 64, middle + ROUND))
        self.assertIn("signature does not match", self.check(middle, middle + ROUND)["chain_error"])
        with connect() as db:
            db.execute("DELETE FROM footage_chain_signatures WHERE upto_seq=?", (middle + ROUND,))  # That signature gone: the stretch runs to the next one
            db.execute("DELETE FROM footage_chain WHERE seq=?", (middle + 2,))
        result = self.check(middle, middle + ROUND)
        self.assertFalse(result["valid"])
        self.assertIn("missing from the chain", result["chain_error"])

    def test_footage_the_nvr_removed_is_expected_and_footage_deleted_outside_it_is_missing(self):
        ids = self.history(10)
        with connect() as db:
            db.execute("DELETE FROM recording_segments WHERE id=?", (ids[0],))  # Removed by the NVR: its removal is recorded and becomes a link
            name = db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (ids[1],)).fetchone()[0]
        (self.folder.parent / name).unlink()  # And one file deleted behind its back
        self.record(ROUND)
        self.chain()
        result = self.check()
        self.assertEqual({entry["segment_id"]: entry["status"] for entry in result["segments"]}, {ids[1]: "missing", ids[2]: "ok"})
