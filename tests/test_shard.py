"""tests/shard.py splits the unit tests over CI's runners: every module in exactly one shard, the shards about even, and the same split on every runner."""
from __future__ import annotations

import unittest

from tests import shard


class ShardTests(unittest.TestCase):
    def test_every_module_is_in_exactly_one_shard(self):
        for count in (1, 2, 4, 7):
            with self.subTest(count=count):
                given = [name for part in shard.shards(count) for name in part]
                self.assertEqual(sorted(given), shard.modules())

    def test_this_module_is_found(self):
        self.assertIn("tests.test_shard", shard.modules())

    def test_the_split_is_the_same_every_time(self):
        self.assertEqual(shard.shards(4), shard.shards(4))

    def test_the_longest_modules_are_spread_and_the_shards_end_about_together(self):
        seconds = {"a": 100.0, "b": 90.0, "c": 50.0, "d": 40.0, "e": 30.0, "f": 20.0, "g": 10.0}
        split = shard.shards(2, list(seconds), seconds)
        totals = sorted(sum(seconds[name] for name in part) for part in split)
        self.assertEqual(totals, [170.0, 170.0])
        self.assertTrue(all(len(part) for part in split))

    def test_a_new_module_without_a_timing_counts_as_the_median(self):
        split = shard.shards(2, ["old1", "old2", "old3", "new"], {"old1": 10.0, "old2": 1.0, "old3": 1.0})
        self.assertIn("new", [name for part in split for name in part])

    def test_the_timings_cover_almost_every_module(self):
        # Shards stay even only while the timings are current: regenerate them from a CI log or with --measure when many modules lack one.
        missing = [name for name in shard.modules() if name not in shard.timings()]
        self.assertLess(len(missing), 20, missing)

    def test_timings_are_read_from_a_ci_log(self):
        log = "\n".join([
            "2026-11-21T05:43:45.0000000Z test_one (tests.test_alpha.Case.test_one) ... ok",
            "2026-11-21T05:43:47.5000000Z test_two (tests.test_alpha.Case.test_two) ... ok",
            "2026-11-21T05:43:48.0000000Z some log output in between",
            "2026-11-21T05:43:50.5000000Z test_three (tests.test_beta.Case.test_three) ... ok",
        ])
        self.assertEqual(shard.seconds_from_log(log), {"tests.test_alpha": 2.5, "tests.test_beta": 3.0})


if __name__ == "__main__":
    unittest.main()
