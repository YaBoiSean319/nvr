import unittest
from datetime import datetime, timezone

from app.scheduling import mode_at


class SchedulingTests(unittest.TestCase):
    def test_schedule_boundaries_are_start_inclusive_and_end_exclusive(self):
        schedule = [{"days": [0], "start": "08:00", "end": "17:00", "mode": "continuous"}]
        self.assertEqual(mode_at(schedule, "UTC", datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)), "continuous")
        self.assertEqual(mode_at(schedule, "UTC", datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc)), "disabled")

    def test_fall_back_repeated_local_hour_is_scheduled_on_both_occurrences(self):
        schedule = [{"days": [6], "start": "01:15", "end": "01:45", "mode": "motion"}]
        first = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
        repeated = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)
        self.assertEqual(mode_at(schedule, "America/New_York", first), "motion")
        self.assertEqual(mode_at(schedule, "America/New_York", repeated), "motion")

    def test_spring_forward_nonexistent_local_minutes_are_skipped(self):
        schedule = [{"days": [6], "start": "02:30", "end": "03:30", "mode": "continuous"}]
        before = datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)  # 01:30 EST
        after = datetime(2026, 3, 8, 7, 15, tzinfo=timezone.utc)  # 03:15 EDT
        self.assertEqual(mode_at(schedule, "America/New_York", before), "disabled")
        self.assertEqual(mode_at(schedule, "America/New_York", after), "continuous")

    def test_overnight_interval_applies_to_the_selected_start_day(self):
        schedule = [{"days": [0], "start": "22:00", "end": "02:00", "mode": "motion"}]
        monday_evening = datetime(2026, 10, 19, 23, 0, tzinfo=timezone.utc)
        tuesday_morning = datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(mode_at(schedule, "UTC", monday_evening), "motion")
        self.assertEqual(mode_at(schedule, "UTC", tuesday_morning), "motion")

    def test_overlapping_intervals_are_rejected(self):
        from app.scheduling import validate_schedule
        with self.assertRaises(ValueError):
            validate_schedule("UTC", [
                {"days": [0], "start": "08:00", "end": "10:00", "mode": "motion"},
                {"days": [0], "start": "09:00", "end": "11:00", "mode": "continuous"},
            ])


if __name__ == "__main__":
    unittest.main()
