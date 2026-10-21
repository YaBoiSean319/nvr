from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.health import recording_health


class RecordingHealthTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
        self.recent = "2026-10-21T11:59:50+00:00"
        self.old = "2026-10-21T11:55:00+00:00"

    def get(self, **overrides):
        values = dict(enabled=True, recording_enabled=True, mode="continuous", effective_mode="continuous",
                      recording_status="recording", motion_source_status="healthy", last_video_at=self.recent,
                      last_footage_write_at=self.recent, now=self.now)
        values.update(overrides)
        return recording_health(**values)

    def test_stale_video_is_uncertain_unless_recorder_reports_camera_failure(self):
        self.assertEqual(self.get(last_video_at=self.old)["state"], "uncertain")
        self.assertEqual(self.get(last_video_at=self.old, recording_status="reconnecting")["state"], "camera_unreachable")

    def test_camera_reconnection_restores_covered_state(self):
        failed = self.get(last_video_at=self.old, recording_status="reconnecting")
        recovered = self.get(recording_status="recording")
        self.assertEqual(failed["state"], "camera_unreachable")
        self.assertEqual(recovered["state"], "recording")
        self.assertTrue(recovered["video_fresh"])

    def test_fresh_video_with_stalled_continuous_write_is_failure(self):
        result = self.get(last_footage_write_at=self.old)
        self.assertEqual(result["state"], "write_stalled")
        self.assertEqual(result["coverage_kind"], "failure")

    def test_motion_idle_is_expected_when_video_and_event_source_are_healthy(self):
        result = self.get(mode="motion", effective_mode="motion", last_footage_write_at=self.old)
        self.assertEqual(result["state"], "expected_idle")
        self.assertEqual(result["coverage_kind"], "expected_idle")

    def test_motion_source_degradation_makes_coverage_uncertain(self):
        result = self.get(mode="motion", effective_mode="motion", motion_source_status="unavailable")
        self.assertEqual(result["state"], "motion_source_degraded")
        self.assertEqual(result["coverage_kind"], "uncertain")

    def test_disabled_policy_is_expected_idle(self):
        self.assertEqual(self.get(effective_mode="disabled")["state"], "expected_idle")


if __name__ == "__main__":
    unittest.main()
