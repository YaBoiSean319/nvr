"""Inactivity rules (#254): an object that has not moved for a set time."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import rules
from app.rules import PathPoint
from app.store import connect, initialize

START = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
BOX = {"x": 0, "y": 0, "width": 0.1, "height": 0.2}


def walk(*steps: tuple[int, float, float]) -> list[PathPoint]:
    """A track seen at (seconds, x, y) points."""
    return [PathPoint(index, START + timedelta(seconds=seconds), x, y, BOX) for index, (seconds, x, y) in enumerate(steps, 1)]


class InactivityTests(unittest.TestCase):
    def moments(self, path, **condition):
        return [point.at - START for point in rules._inactivity(condition, path)]

    def test_fires_once_when_the_object_has_stayed_put_for_the_set_time(self):
        path = walk(*[(second, 0.5 + 0.001 * (second % 2), 0.5) for second in range(0, 120, 5)])
        self.assertEqual(self.moments(path, still_seconds=60), [timedelta(seconds=60)])

    def test_an_object_that_keeps_moving_never_fires(self):
        path = walk(*[(second, 0.1 + 0.02 * (second // 5), 0.5) for second in range(0, 60, 5)])
        self.assertEqual(self.moments(path, still_seconds=20), [])

    def test_moving_restarts_the_clock_and_it_can_fire_again_after(self):
        path = walk((0, 0.2, 0.5), (30, 0.2, 0.5), (35, 0.6, 0.5), (60, 0.6, 0.5), (90, 0.6, 0.5))
        self.assertEqual(self.moments(path, still_seconds=25), [timedelta(seconds=30), timedelta(seconds=60)])

    def test_with_a_zone_only_stillness_inside_it_counts(self):
        path = walk((0, 0.2, 0.5), (60, 0.2, 0.5), (70, 0.8, 0.5), (140, 0.8, 0.5))
        zone = {"x": 0.7, "y": 0, "width": 0.3, "height": 1}
        self.assertEqual(self.moments(path, zone=zone, still_seconds=60), [timedelta(seconds=140)])


class InactivityMigrationTests(unittest.TestCase):
    def test_the_rules_table_accepts_the_condition_and_keeps_existing_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = os.environ.get("NVR_DB_PATH")
            os.environ["NVR_DB_PATH"] = str(Path(directory) / "state.sqlite3")
            try:
                initialize()
                with connect() as db:
                    db.execute("INSERT INTO rules(name,condition_type,condition_json,classes_json,active_from) VALUES('Still','inactivity','{\"still_seconds\":60}','[\"person\"]','2026-11-06T00:00:00+00:00')")
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute("INSERT INTO rules(name,condition_type,condition_json,classes_json,active_from) VALUES('Bad','nonsense','{}','[]','2026-11-06T00:00:00+00:00')")
            finally:
                if previous is None:
                    os.environ.pop("NVR_DB_PATH", None)
                else:
                    os.environ["NVR_DB_PATH"] = previous


if __name__ == "__main__":
    unittest.main()
