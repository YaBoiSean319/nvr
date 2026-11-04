"""Where one recorded file meets the next in an export: no skipped footage, no black frame (#232 follow-up: the car "jumping" in a grid export)."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app import exports

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
START = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)


def segment(number: int, folder: str, starts: float, seconds: float) -> dict:
    return {"id": number, "relative_path": f"{folder}/segment-{number}.mp4",
            "starts_at": (START + timedelta(seconds=starts)).isoformat(), "ends_at": (START + timedelta(seconds=starts + seconds)).isoformat(), "size_bytes": 1}


class ContiguousSegmentsTests(unittest.TestCase):
    def test_a_file_of_one_run_that_starts_up_to_two_seconds_before_the_last_ended_is_placed_end_to_end(self):
        # Each file is named a second before the previous one's end (the name is the whole second it was opened).
        placed = exports._contiguous_segments([segment(1, "run", 0, 10.13), segment(2, "run", 9, 10.0), segment(3, "run", 19, 10.0)])
        self.assertEqual([(item["starts_at"], item["ends_at"]) for item in placed], [
            (segment(1, "run", 0, 10.13)["starts_at"], segment(1, "run", 0, 10.13)["ends_at"]),
            ((START + timedelta(seconds=10.13)).isoformat(), (START + timedelta(seconds=20.13)).isoformat()),
            ((START + timedelta(seconds=20.13)).isoformat(), (START + timedelta(seconds=30.13)).isoformat()),
        ])

    def test_a_real_gap_a_real_overlap_or_another_run_keeps_its_own_times(self):
        for second in (segment(2, "run", 10.5, 10.0), segment(2, "run", 15, 10.0), segment(2, "run", 3, 10.0), segment(2, "reconnected", 9, 10.0), segment(2, "run", 9.5, 10.0)):  # The last: not a recorder name (a part of a second)
            with self.subTest(second["relative_path"], starts=second["starts_at"]):
                placed = exports._contiguous_segments([segment(1, "run", 0, 10.0), second])
                self.assertEqual((placed[1]["starts_at"], placed[1]["ends_at"]), (second["starts_at"], second["ends_at"]))

    def test_the_input_is_not_changed_and_order_is_by_time(self):
        original = [segment(2, "run", 9, 10.0), segment(1, "run", 0, 10.13)]
        before = [dict(item) for item in original]
        placed = exports._contiguous_segments(original)
        self.assertEqual(original, before)
        self.assertEqual([item["id"] for item in placed], [1, 2])


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg is required")
class GridSeamTests(unittest.TestCase):
    def test_a_grid_export_across_file_seams_has_no_black_frame_and_no_jump(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "run").mkdir()
            # A camera sending 15 frames a second with a two-second keyframe interval and B-frames, cut into files the way the recorder cuts it (stream copy).
            subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=15:d=14", "-c:v", "libx264", "-g", "30", "-bf", "2", "-pix_fmt", "yuv420p", str(root / "source.mp4")], check=True, timeout=60)
            subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(root / "source.mp4"), "-c", "copy", "-f", "segment", "-segment_time", "4", "-reset_timestamps", "1",
                            "-segment_list", str(root / "list.csv"), "-segment_list_type", "csv", str(root / "run" / "part%d.mp4")], check=True, timeout=60)
            rows = [line.split(",") for line in (root / "list.csv").read_text().splitlines()]
            self.assertGreaterEqual(len(rows), 3)
            segments, opened = [], 0.0
            for number, (name, first, last) in enumerate(rows, 1):
                length = float(last) - float(first)
                # Named after the whole second it was opened: a second early.
                segments.append({"id": number, "relative_path": f"run/{name}", "starts_at": (START + timedelta(seconds=max(0.0, int(opened) - (1 if number > 1 else 0)))).isoformat(),
                                 "ends_at": (START + timedelta(seconds=max(0.0, int(opened) - (1 if number > 1 else 0)) + length)).isoformat(), "size_bytes": 1})
                opened += length
            segments = exports._contiguous_segments(segments)
            start, end = START, START + timedelta(seconds=sum(float(last) - float(first) for _, first, last in rows))
            spans = exports._unique_output_spans(start, end, segments)
            cameras = [{"camera_id": "a", "name": "Camera", "spans": spans, "privacy_zones": []}]
            intervals = exports._union_intervals(cameras, start, end)

            class Manager:
                async def _run_ffmpeg(self, job_id, args):
                    subprocess.run([FFMPEG, *args], check=True, capture_output=True, timeout=120)

            with patch.object(exports, "recording_file_path", lambda relative: root / relative):
                output = asyncio.run(exports._render_grid(Manager(), "job", root, cameras, ["a"], intervals, "1080p", {"time_zone": "UTC", "date_format": "iso", "clock_format": "24"}))
            raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(output), "-vf", "crop=960:540:0:100,scale=160:90,format=gray", "-f", "rawvideo", "-"], capture_output=True, check=True, timeout=60).stdout
            frames = np.frombuffer(raw, np.uint8).reshape(-1, 90, 160).astype(np.float32)
            black = [index for index in range(len(frames)) if frames[index].mean() < 20]
            jumps = [index for index in range(1, len(frames)) if np.abs(frames[index] - frames[index - 1]).mean() > 25]
            self.assertEqual(black, [], "a black frame at a seam")
            self.assertEqual(jumps, [], "the picture jumps at a seam")
            self.assertAlmostEqual(len(frames) / 15, (end - start).total_seconds(), delta=0.35)


if __name__ == "__main__":
    unittest.main()
