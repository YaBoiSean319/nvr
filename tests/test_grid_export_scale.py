"""A grid export of a long range starts, and its memory does not grow with the number of recorded files (#356).

The grid used to give FFmpeg one input per file and decode them all at once (about 64 MB each for 1080p files), and above about 400
files the command was too long to start. Each tile now reads its camera's files through one list, a tile at a time."""
from __future__ import annotations

import asyncio
import resource
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import exports

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
START = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
FILES = 400
SECONDS_EACH = 0.4
MEMORY_BOUND_MB = 700


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg is required")
class GridScaleTests(unittest.TestCase):
    def test_four_hundred_files_in_a_grid_start_and_stay_within_a_memory_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "run").mkdir()
            # 640x360 at 15 frames a second, a keyframe every 6 frames, cut into files of 0.4 s the way the recorder cuts (stream copy).
            subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=15:d={FILES * SECONDS_EACH}", "-c:v", "libx264", "-preset", "ultrafast",
                            "-g", "6", "-pix_fmt", "yuv420p", str(root / "source.mp4")], check=True, timeout=120)
            subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(root / "source.mp4"), "-c", "copy", "-f", "segment", "-segment_time", str(SECONDS_EACH), "-reset_timestamps", "1",
                            "-segment_list", str(root / "list.csv"), "-segment_list_type", "csv", str(root / "run" / "part%04d.mp4")], check=True, timeout=120)
            rows = [line.split(",") for line in (root / "list.csv").read_text().splitlines()]
            self.assertGreaterEqual(len(rows), FILES - 5)
            segments = []
            for number, (name, first, last) in enumerate(rows, 1):
                begins = START + timedelta(seconds=float(first))
                segments.append({"id": number, "relative_path": f"run/{name}", "starts_at": begins.isoformat(),
                                 "ends_at": (begins + timedelta(seconds=float(last) - float(first))).isoformat(), "size_bytes": 1})
            end = START + timedelta(seconds=float(rows[-1][2]))
            spans = exports._unique_output_spans(START, end, exports._contiguous_segments(segments))
            # Two cameras over the same files, each with a gap, as four tiles: every tile's list is long.
            cameras = [{"camera_id": camera, "name": camera, "spans": [span for index, span in enumerate(spans) if index % 50 != offset], "privacy_zones": []}
                       for offset, camera in enumerate(("a", "b"))]
            intervals = exports._union_intervals(cameras, START, end)
            commands: list[list[str]] = []

            class Manager:
                async def _run_ffmpeg(self, job_id, args):
                    commands.append(args)
                    subprocess.run([FFMPEG, *args], check=True, capture_output=True, timeout=600)

            before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            with patch.object(exports, "recording_file_path", lambda relative: root / relative):
                output = asyncio.run(exports._render_grid(Manager(), "job", root, cameras, ["a", "b", None, "a"], intervals, "1080p",
                                                          {"time_zone": "UTC", "date_format": "iso", "clock_format": "24"}))
            peak_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
            seconds = float(subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(output)],
                                           capture_output=True, text=True, check=True).stdout)
        self.assertAlmostEqual(seconds, (end - START).total_seconds(), delta=0.5)
        self.assertLess(peak_mb, MEMORY_BOUND_MB, f"an FFmpeg process used {peak_mb:.0f} MB (before: {before / 1024:.0f} MB)")
        self.assertTrue(all(sum(1 for arg in command if arg == "-i") <= 4 for command in commands), "a command opened more than four inputs")
        self.assertTrue(all(len(" ".join(command)) < 8000 for command in commands), "a command line grew with the number of files")



class OpenFileLimitTests(unittest.TestCase):
    def test_the_limit_is_raised_to_what_the_system_allows(self):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        if hard != resource.RLIM_INFINITY and hard > 512:
            resource.setrlimit(resource.RLIMIT_NOFILE, (512, hard))
            self.assertEqual(exports.max_open_sources(), hard - exports.OPEN_FILE_RESERVE)
            self.assertEqual(resource.getrlimit(resource.RLIMIT_NOFILE)[0], hard)

    def test_an_export_needing_more_open_files_than_allowed_is_refused_up_front_in_plain_words(self):
        with patch.object(exports, "max_open_sources", return_value=1000):
            self.assertIsNone(exports._too_many_files(1000))
            message = exports._too_many_files(1100)
        self.assertIn("1100 recorded files", message)
        self.assertIn("shorter time or fewer cameras", message)


if __name__ == "__main__":
    unittest.main()
