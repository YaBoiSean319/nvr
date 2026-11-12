"""The canonical stored timestamp (#284): UTC, milliseconds, Z, and in step with what SQLite writes for column defaults."""
from __future__ import annotations

import re
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from app import clock

CANONICAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class StampTests(unittest.TestCase):
    def test_the_form_is_fixed_width_utc_milliseconds_and_z(self):
        for value in (datetime(2026, 11, 12, 9, 30, 0, tzinfo=timezone.utc), datetime(2026, 1, 2, 3, 4, 5, 6789, tzinfo=timezone.utc),
                      datetime(2026, 11, 12, 9, 30, 0, 999999, tzinfo=timezone.utc)):
            self.assertRegex(clock.stamp(value), CANONICAL)
        self.assertEqual(clock.stamp(datetime(2026, 11, 12, 9, 30, 0, 123456, tzinfo=timezone.utc)), "2026-11-12T09:30:00.123Z")
        self.assertEqual(clock.stamp(datetime(2026, 11, 12, 9, 30, 0, 999999, tzinfo=timezone.utc)), "2026-11-12T09:30:00.999Z")  # Truncated, never rounded up into the next second

    def test_another_offset_is_converted_and_a_naive_time_counts_as_utc(self):
        east = datetime(2026, 11, 12, 11, 30, 0, 500000, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(clock.stamp(east), "2026-11-12T09:30:00.500Z")
        self.assertEqual(clock.stamp(datetime(2026, 11, 12, 9, 30, 0)), "2026-11-12T09:30:00.000Z")

    def test_comparing_stamps_as_text_orders_them_in_time(self):
        start = datetime(2026, 11, 12, 9, 59, 59, 995000, tzinfo=timezone.utc)
        moments = [start + timedelta(milliseconds=step * 3) for step in range(12)]  # Across a second, a minute and an hour
        self.assertEqual([clock.stamp(item) for item in moments], sorted(clock.stamp(item) for item in moments))

    def test_a_stamp_is_read_back_by_parse_and_by_sqlite(self):
        moment = datetime(2026, 11, 12, 9, 30, 0, 123000, tzinfo=timezone.utc)
        self.assertEqual(clock.parse(clock.stamp(moment)), moment)
        db = sqlite3.connect(":memory:")
        self.assertEqual(db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', ?)", (clock.stamp(moment),)).fetchone()[0], clock.stamp(moment))

    def test_the_database_defaults_are_the_same_form(self):
        self.assertRegex(sqlite3.connect(":memory:").execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0], CANONICAL)
        self.assertRegex(clock.stamp(), CANONICAL)


if __name__ == "__main__":
    unittest.main()
