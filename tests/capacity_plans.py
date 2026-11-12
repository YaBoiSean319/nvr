"""Plans whose exact estimate is pinned by tests/fixtures/capacity_golden.json (#286): varied enough to reach every branch of capacity.estimate."""
from __future__ import annotations

from app.capacity import Row

PLANS: dict[str, dict] = {
    "empty": {"rows": [], "viewers": 3, "tiles": 4, "overrides": None},
    "one_continuous_default_picture": {"rows": [Row(name="Gate")], "viewers": 1, "tiles": 1, "overrides": None},
    "continuous_entered_bitrate": {"rows": [Row(name="Lobby", count=4, width=2560, height=1440, codec="h264", fps=20, bitrate_kbps=4096, retention_days=14)], "viewers": 2, "tiles": 4, "overrides": None},
    "motion_assumed_share": {"rows": [Row(name="Yard", count=2, recording="motion", retention_days=7)], "viewers": 0, "tiles": 0, "overrides": None},
    "motion_measured_share": {"rows": [Row(name="Dock", recording="motion", recorded_share=0.12, bitrate_kbps=1500, bitrate_source="measured", width=1280, height=720, fps=10)], "viewers": 1, "tiles": 2, "overrides": None},
    "disabled_recording": {"rows": [Row(name="Spare", count=3, recording="disabled", width=1920, height=1080, codec="h264", fps=15)], "viewers": 1, "tiles": 1, "overrides": None},
    "hevc_mixed": {"rows": [Row(name="North", count=5, width=3840, height=2160, codec="h265", fps=15), Row(name="South", count=3, width=1920, height=1080, codec="h264", fps=15)], "viewers": 4, "tiles": 8, "overrides": None},
    "ai_with_and_without_rate": {"rows": [Row(name="AI a", count=6, ai=True, analysis_fps=2.0), Row(name="AI b", count=4, ai=True), Row(name="Plain", count=2)], "viewers": 2, "tiles": 4, "overrides": None},
    "assumed_notes_carried": {"rows": [Row(name="Unknown", count=2, assumed=["retention (30 days)"], retention_days=30)], "viewers": 1, "tiles": 1, "overrides": None},
    "overrides_changed_and_unchanged_and_unknown": {"rows": [Row(name="Gate", count=2, recording="motion", ai=True, codec="hevc")], "viewers": 2, "tiles": 3,
                                                     "overrides": {"motion_share": 0.5, "overhead": 1.1, "live_share": 0.25, "not_an_assumption": 9}},
    "more_tiles_than_cameras": {"rows": [Row(name="Few", count=2)], "viewers": 5, "tiles": 50, "overrides": None},
    "everything_at_once": {"rows": [Row(name="A", count=10, width=1920, height=1080, codec="h265", fps=25, recording="motion", recorded_share=0.3, ai=True, analysis_fps=1.5, retention_days=90),
                                    Row(name="B", count=7, recording="continuous", ai=True), Row(name="C", count=1, recording="disabled", codec="h265", ai=False),
                                    Row(name="D", count=3, width=640, height=480, bitrate_kbps=512, bitrate_source="entered", recording="motion")], "viewers": 6, "tiles": 12,
                           "overrides": {"hevc_transcode_share": 0.6, "inference_ms_cpu": 90}},
}
