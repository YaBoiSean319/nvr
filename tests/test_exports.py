from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.exports import ExportManager, create_export_record, expire_exports, recover_interrupted_exports
from app.main import app, current_user
from app.retention import cleanup_expired_recordings, playback_is_leased
from app.store import connect, initialize


class ExportJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_STORAGE_VOLUMES")}
        self.root = Path(self.temporary.name) / "recordings"
        self.root.mkdir()
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root)
        os.environ.pop("NVR_STORAGE_VOLUMES", None)
        initialize()
        self.camera = str(uuid.uuid4())
        self.user_id = 1
        self.other_user_id = 2
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'exporter','unused','operator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(2,'second','unused','operator')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (self.camera, "Export camera"))
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        if self.ffmpeg:
            media_dir = self.root / self.camera
            media_dir.mkdir(parents=True)
            self.media = media_dir / "clip.mp4"
            subprocess.run([
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10",
                "-t", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(self.media),
            ], check=True, timeout=30)
        else:
            self.media = self.root / self.camera / "clip.mp4"
            self.media.parent.mkdir(parents=True)
            self.media.write_bytes(b"fake video")
        with connect() as db:
            self.segment_id = db.execute(
                "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                (self.camera, f"{self.camera}/clip.mp4", "2026-10-20T10:00:00+00:00", "2026-10-20T10:00:01+00:00", self.media.stat().st_size),
            ).lastrowid
        app.dependency_overrides[current_user] = lambda: {"id": self.user_id, "username": "exporter", "role": "operator"}
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        app.dependency_overrides.pop(current_user, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def _create(self, actor_id: int | None = None) -> str:
        if actor_id is not None and actor_id != self.user_id:
            app.dependency_overrides[current_user] = lambda: {"id": actor_id, "username": "second", "role": "operator"}
        response = self.client.post("/api/exports", json={
            "camera_ids": [self.camera],
            "start": "2026-10-20T09:59:59.500000Z",
            "end": "2026-10-20T10:00:00.800000Z",
        })
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["id"]

    def _wait_for_status(self, job_id: str, *terminal: str) -> dict:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/exports/{job_id}")
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            if result["status"] in terminal:
                return result
            time.sleep(0.05)
        self.fail(f"Export {job_id} did not reach {terminal}: {result}")

    def _video_frame(self, path: Path, at: float, width: int, height: int) -> bytes:
        return subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", str(path),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ], capture_output=True, check=True, timeout=20).stdout

    def _save_rgb_png(self, frame: bytes, width: int, height: int, destination: Path) -> None:
        self.assertEqual(len(frame), width * height * 3)
        subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pixel_format", "rgb24",
            "-video_size", f"{width}x{height}", "-i", "pipe:0", "-frames:v", "1",
            "-compression_level", "9", "-y", str(destination),
        ], input=frame, check=True, timeout=30)

    def _bright_text_rows(self, frame: bytes, width: int, x_start: int, x_end: int,
                          y_start: int, y_end: int, threshold: int = 195,
                          minimum_pixels: int = 3) -> list[tuple[int, int]]:
        """Return rendered white-glyph row runs from a decoded synthetic frame."""
        rows = []
        for y in range(y_start, y_end):
            bright = 0
            offset = y * width * 3
            for x in range(x_start, x_end):
                pixel = offset + x * 3
                if min(frame[pixel:pixel + 3]) > threshold:
                    bright += 1
            if bright >= minimum_pixels:
                rows.append(y)
        runs: list[list[int]] = []
        for row in rows:
            if not runs or row > runs[-1][-1] + 2:
                runs.append([row])
            else:
                runs[-1].append(row)
        return [(run[0], run[-1]) for run in runs]

    def _bright_pixel_bounds(self, frame: bytes, width: int, x_start: int, x_end: int,
                             y_start: int, y_end: int,
                             threshold: int = 195) -> tuple[int, int, int, int] | None:
        points = []
        for y in range(y_start, y_end):
            offset = y * width * 3
            for x in range(x_start, x_end):
                pixel = offset + x * 3
                if min(frame[pixel:pixel + 3]) > threshold:
                    points.append((x, y))
        if not points:
            return None
        return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)

    def test_export_is_playable_and_metadata_lists_actual_and_missing_intervals(self):
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("FFmpeg and ffprobe are required for the playable export check")
        job_id = self._create()
        with connect() as db:
            db.execute("INSERT INTO recording_holds(id,segment_id,camera_id,created_by) VALUES(?,?,?,?)", (str(uuid.uuid4()), self.segment_id, self.camera, self.user_id))
        result = self._wait_for_status(job_id, "completed", "failed")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["progress"], 100)
        self.assertIsNotNone(result["expires_at"])
        self.assertEqual(result["output_modes"], ["individual", "grid"])
        self.assertFalse(result["include_audio"])
        download = self.client.get(result["download_url"])
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(download.headers["content-type"], "application/zip")
        package = Path(self.temporary.name) / "result.zip"
        package.write_bytes(download.content)
        with zipfile.ZipFile(package) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            self.assertEqual(metadata["expires_at"], result["expires_at"])
            video_name = metadata["cameras"][0]["video_file"]
            self.assertIn(video_name, archive.namelist())
            self.assertTrue(video_name.startswith("camera-export-camera-01-"))
            integrity = json.loads(archive.read("integrity.json"))
            self.assertEqual({item["path"] for item in integrity["files"]}, {"metadata.json", video_name, "grid.mp4"})
            exported_video = Path(self.temporary.name) / "camera.mp4"
            exported_video.write_bytes(archive.read(video_name))
        self.assertEqual(metadata["cameras"][0]["actual_intervals"], [{"start": "2026-10-20T10:00:00+00:00", "end": "2026-10-20T10:00:00.800000+00:00"}])
        self.assertEqual(metadata["cameras"][0]["overlay"]["first_source_labels"], ["Oct 20, 2026 10:00:00 AM"])
        self.assertEqual(metadata["cameras"][0]["missing_intervals"], [
            {"start": "2026-10-20T09:59:59.500000+00:00", "end": "2026-10-20T10:00:00+00:00"},
        ])
        probe = subprocess.run([self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(exported_video)], capture_output=True, check=True, timeout=15)
        self.assertGreater(float(json.loads(probe.stdout)["format"]["duration"]), 0)
        self.assertFalse(playback_is_leased(self.media))
        with connect() as db:
            actions = [row[0] for row in db.execute("SELECT action FROM audit_log WHERE resource_id=? ORDER BY id", (job_id,))]
            hold = db.execute("SELECT 1 FROM recording_holds WHERE segment_id=? AND released_at IS NULL", (self.segment_id,)).fetchone()
        self.assertEqual(actions, ["export_requested", "export_completed"])
        self.assertIsNotNone(hold, "export must retain active preservation holds")

    def test_individual_overlay_uses_compact_bounded_labels_and_keeps_full_identity(self):
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("FFmpeg and ffprobe are required for rendered overlay checks")
        full_name = "Synthetic North Hall Camera " + "Long Name " * 14
        with connect() as db:
            db.execute("UPDATE cameras SET name=? WHERE id=?", (full_name, self.camera))
        accepted = self.client.post("/api/exports", json={
            "camera_ids": [self.camera], "output_modes": ["individual"],
            "start": "2026-10-20T10:00:00Z", "end": "2026-10-20T10:00:01Z",
        })
        self.assertEqual(accepted.status_code, 202, accepted.text)
        result = self._wait_for_status(accepted.json()["id"], "completed", "failed")
        self.assertEqual(result["status"], "completed", result)
        with zipfile.ZipFile(io.BytesIO(self.client.get(result["download_url"]).content)) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            exported_video = Path(self.temporary.name) / "long-name-camera.mp4"
            exported_video.write_bytes(archive.read(metadata["cameras"][0]["video_file"]))
        self.assertEqual(metadata["cameras"][0]["camera_name"], full_name)
        self.assertEqual(metadata["cameras"][0]["overlay"]["first_source_labels"], ["Oct 20, 2026 10:00:00 AM"])
        frame = self._video_frame(exported_video, 0.2, 1280, 720)
        text_rows = self._bright_text_rows(frame, 1280, 0, 1280, 0, 220)
        line_heights = [bottom - top + 1 for top, bottom in text_rows]
        self.assertEqual(len(line_heights), 2, f"only the bounded camera name and full timestamp should appear at the top: {text_rows}")
        self.assertGreaterEqual(min(line_heights), 16, f"720p overlay glyphs should be at least 16 px high: {line_heights}")
        self.assertLessEqual(max(line_heights), 20, f"720p glyphs should stay within the 16–20 px target: {line_heights}")
        bright_x = [x for y in range(220) for x in range(1280)
                    if min(frame[(y * 1280 + x) * 3:(y * 1280 + x) * 3 + 3]) > 195]
        self.assertLess(max(bright_x), 900, "long camera names should be bounded instead of spanning most of the frame")

    def test_individual_overlay_comparison_emits_sanitized_raster_evidence(self):
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("FFmpeg and ffprobe are required for rendered overlay evidence")

        width, height = 1280, 720
        full_name = "Synthetic Camera " + "Long Name " * 10
        later = self.root / self.camera / "later-generated.mp4"
        subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=black:s=64x64:r=10", "-t", "1", "-an", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-y", str(later),
        ], check=True, timeout=30)
        with connect() as db:
            db.execute("UPDATE cameras SET name=? WHERE id=?", (full_name, self.camera))
            db.execute(
                "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                (self.camera, f"{self.camera}/later-generated.mp4", "2026-10-20T10:00:03+00:00",
                 "2026-10-20T10:00:04+00:00", later.stat().st_size),
            )
        app.dependency_overrides[current_user] = lambda: {"id": self.user_id, "username": "exporter", "role": "administrator"}
        configured = self.client.put("/api/settings/display", json={
            "time_zone": "America/New_York", "date_format": "iso", "clock_format": "24",
        })
        self.assertEqual(configured.status_code, 200, configured.text)
        app.dependency_overrides[current_user] = lambda: {"id": self.user_id, "username": "exporter", "role": "operator"}

        accepted = self.client.post("/api/exports", json={
            "camera_ids": [self.camera], "output_modes": ["individual"],
            "start": "2026-10-20T10:00:00Z", "end": "2026-10-20T10:00:04Z",
        })
        self.assertEqual(accepted.status_code, 202, accepted.text)
        result = self._wait_for_status(accepted.json()["id"], "completed", "failed")
        self.assertEqual(result["status"], "completed", result)
        with zipfile.ZipFile(io.BytesIO(self.client.get(result["download_url"]).content)) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            camera_metadata = metadata["cameras"][0]
            exported_video = Path(self.temporary.name) / "generated-after.mp4"
            exported_video.write_bytes(archive.read(camera_metadata["video_file"]))

        self.assertEqual(camera_metadata["camera_name"], full_name)
        self.assertEqual(camera_metadata["actual_intervals"], [
            {"start": "2026-10-20T10:00:00+00:00", "end": "2026-10-20T10:00:01+00:00"},
            {"start": "2026-10-20T10:00:03+00:00", "end": "2026-10-20T10:00:04+00:00"},
        ])
        self.assertEqual(camera_metadata["overlay"]["first_source_labels"], [
            "2026-10-20 06:00:00", "2026-10-20 06:00:03",
        ])

        before_srt = Path(self.temporary.name) / "legacy-overlay.srt"
        before_srt.write_text(
            "1\n00:00:00,000 --> 00:00:01,600\n"
            f"{{\\an7}}{full_name}\n{camera_metadata['overlay']['first_source_labels'][1]}\n"
            "Recording gap skipped\n\n",
            encoding="utf-8",
        )
        escaped_srt = before_srt.as_posix().replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        legacy_style = (
            "Alignment=7,Fontsize=24,BorderStyle=3,Outline=1,Shadow=0,MarginL=18,MarginV=14,"
            "PrimaryColour=&H00FFFFFF,BackColour=&H80000000"
        )
        legacy_filter = f"subtitles=filename='{escaped_srt}':force_style='{legacy_style}'"
        before_frame = subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            f"color=c=black:s={width}x{height}:r=10:d=1.6", "-vf", legacy_filter,
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ], capture_output=True, check=True, timeout=30).stdout
        after_frame = self._video_frame(exported_video, 1.2, width, height)

        evidence_dir = Path(__file__).resolve().parents[1] / "test-results" / "export-overlay-evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        evidence_dir.chmod(0o700)

        before_png = evidence_dir / "overlay-before.png"
        after_png = evidence_dir / "overlay-after.png"
        comparison_png = evidence_dir / "overlay-comparison.png"
        self._save_rgb_png(before_frame, width, height, before_png)
        self._save_rgb_png(after_frame, width, height, after_png)
        subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(before_png), "-i", str(after_png),
            "-filter_complex", "hstack=inputs=2", "-frames:v", "1", "-y", str(comparison_png),
        ], check=True, timeout=30)

        baseline_rows = self._bright_text_rows(before_frame, width, 0, width, 0, height)
        current_rows = self._bright_text_rows(after_frame, width, 0, width, 0, 220)
        gap_rows = self._bright_text_rows(after_frame, width, 0, width, 620, height)

        def line_measurements(frame: bytes, rows: list[tuple[int, int]]) -> list[dict[str, object]]:
            return [{
                "bounds_px": list(self._bright_pixel_bounds(frame, width, 0, width, top, bottom + 1)),
                "height_px": bottom - top + 1,
            } for top, bottom in rows]

        baseline_lines = line_measurements(before_frame, baseline_rows)
        current_lines = line_measurements(after_frame, current_rows)
        gap_lines = line_measurements(after_frame, gap_rows)
        self.assertGreaterEqual(len(baseline_lines), 5,
                                f"the legacy long name should wrap, with timestamp and gap notice still in the top cue: {baseline_rows}")
        self.assertEqual(len(current_lines), 2, f"compact overlay should keep only name and timestamp at the top: {current_rows}")
        self.assertEqual(len(gap_lines), 1, f"current gap notice should occupy its own bottom band: {gap_rows}")
        self.assertGreater(max(line["height_px"] for line in baseline_lines), max(line["height_px"] for line in current_lines))
        self.assertLess(max(line["bounds_px"][2] for line in current_lines), 900)

        measurements = {
            "record_type": "synthetic-export-overlay-comparison",
            "frame": {"width_px": width, "height_px": height, "current_output_time_seconds": 1.2},
            "fixture": {"media": "generated black H.264 clips", "camera_label": "synthetic long camera name"},
            "timestamp": camera_metadata["overlay"]["first_source_labels"][1],
            "baseline": {
                "source": "app/exports.py at origin/main",
                "source_commit": "4f09d5354c57244bb4826d33ec4bed804a35188d",
                "font_size": 24,
                "subtitle_style": legacy_style,
                "cue_layout": "unbounded name, timestamp, and Recording gap skipped share one top cue; long name wraps",
                "top_lines": baseline_lines,
            },
            "current": {
                "source": "app/exports.py",
                "name_font_size": 9,
                "timestamp_font_size": 11,
                "cue_layout": "bounded name and timestamp at top; gap notice in bottom band",
                "top_lines": current_lines,
                "gap_band": {"bounds_px": [0, 620, width - 1, height - 1], "text_lines": gap_lines},
            },
            "comparison": {
                "baseline_max_glyph_height_px": max(line["height_px"] for line in baseline_lines),
                "current_max_glyph_height_px": max(line["height_px"] for line in current_lines),
                "baseline_to_current_max_glyph_height_ratio": round(
                    max(line["height_px"] for line in baseline_lines) / max(line["height_px"] for line in current_lines), 3,
                ),
            },
        }
        measurement_path = evidence_dir / "measurements.json"
        measurement_path.write_text(json.dumps(measurements, indent=2) + "\n", encoding="utf-8")
        measurement_path.chmod(0o600)
        self.assertLess(sum(path.stat().st_size for path in evidence_dir.iterdir()), 5 * 1024 * 1024,
                        "retained generated raster evidence should remain compact")

    def test_grid_opening_gap_notice_uses_a_separate_compact_band(self):
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("FFmpeg and ffprobe are required for rendered overlay checks")
        later = self.root / self.camera / "after-opening-gap.mp4"
        subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=green:s=96x54:r=10",
            "-t", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(later),
        ], check=True, timeout=30)
        with connect() as db:
            db.execute(
                "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                (self.camera, f"{self.camera}/after-opening-gap.mp4", "2026-10-20T10:00:02+00:00", "2026-10-20T10:00:03+00:00", later.stat().st_size),
            )
        accepted = self.client.post("/api/exports", json={
            "camera_ids": [self.camera], "tile_camera_ids": [self.camera],
            "output_modes": ["grid"], "grid_resolution": "1080p",
            "start": "2026-10-20T10:00:01.500000Z", "end": "2026-10-20T10:00:03Z",
        })
        self.assertEqual(accepted.status_code, 202, accepted.text)
        result = self._wait_for_status(accepted.json()["id"], "completed", "failed")
        self.assertEqual(result["status"], "completed", result)
        with zipfile.ZipFile(io.BytesIO(self.client.get(result["download_url"]).content)) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            rendered = Path(self.temporary.name) / "opening-gap-grid.mp4"
            rendered.write_bytes(archive.read("grid.mp4"))
        self.assertEqual(metadata["grid"]["global_gaps_compressed"], [{
            "source_start": "2026-10-20T10:00:01.500000+00:00",
            "source_end": "2026-10-20T10:00:02+00:00",
            "output_transition_seconds": 0.0,
            "duration_seconds": 0.5,
        }])
        frame = self._video_frame(rendered, 0.2, 1920, 1080)
        bottom_notice = self._bright_text_rows(frame, 1920, 400, 1520, 980, 1080)
        self.assertEqual(len(bottom_notice), 1, f"the opening gap message should render in its own bottom band: {bottom_notice}")

    def test_grid_gap_notices_and_labels_fit_each_tile_count_and_4k_scale(self):
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("FFmpeg and ffprobe are required for generated grid checks")
        long_name = "Synthetic North Hall Camera " + "Long Name " * 14
        later_source = self.root / "late-source.mp4"
        subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=yellow:s=96x54:r=10",
            "-t", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(later_source),
        ], check=True, timeout=30)
        subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=96x54:r=10",
            "-t", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(self.media),
        ], check=True, timeout=30)
        camera_ids = [self.camera]
        with connect() as db:
            db.execute("UPDATE cameras SET name=? WHERE id=?", (long_name, self.camera))
            db.execute(
                "UPDATE recording_segments SET starts_at=?,ends_at=?,size_bytes=? WHERE id=?",
                ("2026-10-20T10:00:01+00:00", "2026-10-20T10:00:02+00:00", self.media.stat().st_size, self.segment_id),
            )
            for index in range(1, 16):
                camera_id = str(uuid.uuid4())
                camera_ids.append(camera_id)
                db.execute(
                    "INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')",
                    (camera_id, f"Synthetic Camera {index + 1:02d}"),
                )
                camera_dir = self.root / camera_id
                camera_dir.mkdir(parents=True)
                first_path, second_path = camera_dir / "first.mp4", camera_dir / "later.mp4"
                shutil.copyfile(self.media, first_path)
                shutil.copyfile(later_source, second_path)
                db.executemany(
                    "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                    [
                        (camera_id, f"{camera_id}/first.mp4", "2026-10-20T10:00:01+00:00", "2026-10-20T10:00:02+00:00", first_path.stat().st_size),
                        (camera_id, f"{camera_id}/later.mp4", "2026-10-20T10:00:04+00:00", "2026-10-20T10:00:05+00:00", second_path.stat().st_size),
                    ],
                )
            first_later_path = self.root / self.camera / "later.mp4"
            shutil.copyfile(later_source, first_later_path)
            db.execute(
                "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                (self.camera, f"{self.camera}/later.mp4", "2026-10-20T10:00:04+00:00", "2026-10-20T10:00:05+00:00", first_later_path.stat().st_size),
            )

        app.dependency_overrides[current_user] = lambda: {"id": self.user_id, "username": "exporter", "role": "administrator"}
        display = self.client.put("/api/settings/display", json={
            "time_zone": "America/New_York", "date_format": "long", "clock_format": "24",
        })
        self.assertEqual(display.status_code, 200, display.text)
        app.dependency_overrides[current_user] = lambda: {"id": self.user_id, "username": "exporter", "role": "operator"}

        evidence_dir = Path(__file__).resolve().parents[1] / "test-results" / "export-overlay-evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        evidence_dir.chmod(0o700)
        grid_measurements: dict[str, object] = {
            "record_type": "synthetic-grid-overlay-evidence",
            "fixture": "generated solid-color H.264 clips with neutral synthetic camera names",
            "current_frames": [],
            "baseline_comparisons": [],
        }
        legacy_style = (
            "Alignment=7,Fontsize=24,BorderStyle=3,Outline=1,Shadow=0,MarginL=18,MarginV=14,"
            "PrimaryColour=&H00FFFFFF,BackColour=&H80000000"
        )

        def subtitle_filter(path: Path, style: str) -> str:
            escaped = path.as_posix().replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
            return f"subtitles=filename='{escaped}':force_style='{style}'"

        def measure_grid_frame(frame: bytes, frame_width: int, frame_height: int, tile_count: int,
                               metadata: dict, resolution: str) -> dict[str, object]:
            columns = int(tile_count ** 0.5)
            tile_width, tile_height = frame_width // columns, frame_height // columns
            threshold = 145 if (resolution == "4k" and tile_count >= 9) or (resolution == "1080p" and tile_count == 16) else 195
            minimum_pixels = 2 if threshold == 145 else 3
            expected_timestamp = metadata["grid"]["overlays"][camera_ids[0]]["first_source_labels"][1]
            reference_font_size = 10 if tile_height >= 1080 else 9
            reference_margin = max(6, round(reference_font_size * 0.7))
            reference_srt = Path(self.temporary.name) / f"timestamp-reference-{frame_width}-{tile_count}.srt"
            reference_srt.write_text(
                f"1\n00:00:00,000 --> 00:00:01,000\n{{\\an7}}{expected_timestamp}\n\n",
                encoding="utf-8",
            )
            reference_style = (
                f"Alignment=7,Fontsize={reference_font_size},BorderStyle=3,Outline=1,Shadow=0,"
                f"MarginL={reference_margin},MarginV={reference_margin},"
                "PrimaryColour=&H00FFFFFF,BackColour=&H80000000"
            )
            reference_frame = subprocess.run([
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                f"color=c=black:s={tile_width}x{tile_height}:r=15:d=1", "-vf",
                subtitle_filter(reference_srt, reference_style), "-frames:v", "1",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            ], capture_output=True, check=True, timeout=30).stdout
            reference_bounds = self._bright_pixel_bounds(
                reference_frame, tile_width, 0, tile_width, 0, min(tile_height, 160), threshold=threshold,
            )
            self.assertIsNotNone(reference_bounds, f"{resolution} full timestamp reference should render")
            reference_width = reference_bounds[2] - reference_bounds[0] + 1
            tile_records = []
            tile_bounds = []
            for index, camera_id in enumerate(camera_ids[:tile_count]):
                tile_x, tile_y = (index % columns) * tile_width, (index // columns) * tile_height
                region_bottom = tile_y + min(tile_height, 160)
                rows = self._bright_text_rows(
                    frame, frame_width, tile_x, tile_x + tile_width, tile_y, region_bottom,
                    threshold=threshold, minimum_pixels=minimum_pixels,
                )
                self.assertEqual(len(rows), 2,
                                 f"{resolution} {tile_count}-tile camera {index + 1} should render name and full timestamp: {rows}")
                name_bounds = self._bright_pixel_bounds(
                    frame, frame_width, tile_x, tile_x + tile_width, rows[0][0], rows[0][1] + 1, threshold=threshold,
                )
                timestamp_bounds = self._bright_pixel_bounds(
                    frame, frame_width, tile_x, tile_x + tile_width, rows[1][0], rows[1][1] + 1, threshold=threshold,
                )
                label_bounds = self._bright_pixel_bounds(
                    frame, frame_width, tile_x, tile_x + tile_width, tile_y, region_bottom, threshold=threshold,
                )
                self.assertIsNotNone(name_bounds)
                self.assertIsNotNone(timestamp_bounds)
                self.assertIsNotNone(label_bounds)
                local_right = label_bounds[2] - tile_x
                self.assertLess(local_right, int(tile_width * 0.78),
                                f"{resolution} {tile_count}-tile label spills toward a neighboring tile: {label_bounds}")
                self.assertLess(label_bounds[3], tile_y + tile_height,
                                f"{resolution} {tile_count}-tile label extends beyond its tile: {label_bounds}")
                left_edge_margin = label_bounds[0] - tile_x
                right_edge_margin = tile_x + tile_width - 1 - label_bounds[2]
                bottom_edge_margin = tile_y + tile_height - 1 - label_bounds[3]
                overlay = metadata["grid"]["overlays"][camera_id]
                self.assertEqual(overlay["first_source_labels"], [
                    "October 20, 2026 06:00:01", "October 20, 2026 06:00:04",
                ])
                timestamp_width = timestamp_bounds[2] - timestamp_bounds[0] + 1
                timestamp_width_delta = abs(timestamp_width - reference_width)
                self.assertLessEqual(timestamp_width_delta, max(4, round(reference_width * 0.02)),
                                     f"{resolution} {tile_count}-tile timestamp raster should match its complete reference width: "
                                     f"actual={timestamp_width}, reference={reference_width}")
                tile_bounds.append(label_bounds)
                tile_records.append({
                    "tile_index": index,
                    "tile_bounds_px": [tile_x, tile_y, tile_x + tile_width - 1, tile_y + tile_height - 1],
                    "label_bounds_px": list(label_bounds),
                    "name_row": {"bounds_px": list(name_bounds), "height_px": rows[0][1] - rows[0][0] + 1},
                    "timestamp": {
                        "text": overlay["first_source_labels"][1],
                        "bounds_px": list(timestamp_bounds),
                        "height_px": rows[1][1] - rows[1][0] + 1,
                        "reference_width_px": reference_width,
                        "width_delta_from_full_reference_px": timestamp_width_delta,
                        "complete_width_match": True,
                    },
                    "bounded_within_tile": True,
                    "tile_edge_margins_px": {
                        "left": left_edge_margin,
                        "right": right_edge_margin,
                        "bottom": bottom_edge_margin,
                    },
                })

            for index, bounds in enumerate(tile_bounds):
                if index % columns < columns - 1:
                    self.assertLess(bounds[2], tile_bounds[index + 1][0],
                                    f"{resolution} {tile_count}-tile label boxes overlap across adjacent columns")

            gap_band_top = 2050 if resolution == "4k" else 980
            gap_x_start, gap_x_end = ((800, 3040) if resolution == "4k" else (400, 1520))
            gap_rows = self._bright_text_rows(
                frame, frame_width, gap_x_start, gap_x_end, gap_band_top, frame_height,
                threshold=threshold, minimum_pixels=minimum_pixels,
            )
            self.assertEqual(len(gap_rows), 1,
                             f"{resolution} {tile_count}-tile gap notice should remain in its own bottom band: {gap_rows}")
            gap_bounds = self._bright_pixel_bounds(
                frame, frame_width, gap_x_start, gap_x_end, gap_band_top, frame_height, threshold=threshold,
            )
            self.assertIsNotNone(gap_bounds)
            self.assertGreater(gap_bounds[1], max(bounds[3] for bounds in tile_bounds),
                               f"{resolution} {tile_count}-tile gap notice should not overlap tile labels: {gap_bounds}")
            gap_separation = gap_bounds[1] - max(bounds[3] for bounds in tile_bounds)
            return {
                "resolution": resolution,
                "frame_bounds_px": [frame_width, frame_height],
                "tile_count": tile_count,
                "output_time_seconds": 1.2,
                "expected_source_timestamps": ["October 20, 2026 06:00:01", "October 20, 2026 06:00:04"],
                "tiles": tile_records,
                "cross_tile_spill": False,
                "gap_band": {"bounds_px": [0, gap_band_top, frame_width - 1, frame_height - 1],
                              "text_bounds_px": list(gap_bounds), "text_height_px": gap_rows[0][1] - gap_rows[0][0] + 1,
                              "separation_below_tile_labels_px": gap_separation},
            }

        def old_style_grid_frame(tile_count: int, frame_width: int, frame_height: int,
                                 timestamp: str) -> bytes:
            columns = int(tile_count ** 0.5)
            tile_width, tile_height = frame_width // columns, frame_height // columns
            tile_names = [long_name] + [f"Synthetic Camera {index + 1:02d}" for index in range(1, tile_count)]
            graph = []
            for index, name in enumerate(tile_names):
                subtitle_path = Path(self.temporary.name) / f"legacy-grid-{frame_width}-{tile_count}-{index}.srt"
                subtitle_path.write_text(
                    f"{index + 1}\n00:00:00,000 --> 00:00:01,600\n{{\\an7}}{name}\n{timestamp}\n\n",
                    encoding="utf-8",
                )
                graph.append(
                    f"color=c=yellow:s={tile_width}x{tile_height}:r=15:d=1.6,"
                    f"{subtitle_filter(subtitle_path, legacy_style)}[tile{index}]"
                )
            layout = "|".join(f"{index % columns * tile_width}_{index // columns * tile_height}"
                              for index in range(tile_count))
            gap_path = Path(self.temporary.name) / f"legacy-grid-gap-{frame_width}-{tile_count}.srt"
            gap_path.write_text(
                "1\n00:00:00,000 --> 00:00:01,600\nRecording gap skipped\n"
                "October 20, 2026 06:00:02 – October 20, 2026 06:00:04\n\n",
                encoding="utf-8",
            )
            graph.append(
                f"{''.join(f'[tile{index}]' for index in range(tile_count))}"
                f"xstack=inputs={tile_count}:layout={layout}:fill=black,{subtitle_filter(gap_path, legacy_style)}[out]"
            )
            return subprocess.run([
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-filter_complex", ";".join(graph),
                "-map", "[out]", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            ], capture_output=True, check=True, timeout=60).stdout

        first_tile_heights: list[int] = []
        first_global_notice_heights: list[int] = []
        for tile_count in (1, 4, 9, 16):
            accepted = self.client.post("/api/exports", json={
                "camera_ids": camera_ids[:tile_count], "tile_camera_ids": camera_ids[:tile_count],
                "output_modes": ["grid"], "grid_resolution": "1080p",
                "start": "2026-10-20T10:00:00Z", "end": "2026-10-20T10:00:05Z",
            })
            self.assertEqual(accepted.status_code, 202, accepted.text)
            result = self._wait_for_status(accepted.json()["id"], "completed", "failed")
            self.assertEqual(result["status"], "completed", result)
            with zipfile.ZipFile(io.BytesIO(self.client.get(result["download_url"]).content)) as archive:
                metadata = json.loads(archive.read("metadata.json"))
                rendered = Path(self.temporary.name) / f"grid-{tile_count}.mp4"
                rendered.write_bytes(archive.read("grid.mp4"))
            self.assertEqual(metadata["grid"]["duration_seconds"], 2.0)
            self.assertEqual(len(metadata["grid"]["global_gaps_compressed"]), 2)
            self.assertEqual(metadata["grid"]["global_gaps_compressed"][0]["source_start"], "2026-10-20T10:00:00+00:00")
            self.assertEqual(metadata["grid"]["global_gaps_compressed"][0]["source_end"], "2026-10-20T10:00:01+00:00")
            self.assertEqual(metadata["grid"]["global_gaps_compressed"][1]["source_start"], "2026-10-20T10:00:02+00:00")
            self.assertEqual(metadata["grid"]["global_gaps_compressed"][1]["source_end"], "2026-10-20T10:00:04+00:00")
            overlay = metadata["grid"]["overlays"][self.camera]
            self.assertEqual(overlay["first_source_labels"], ["October 20, 2026 06:00:01", "October 20, 2026 06:00:04"])
            self.assertTrue(overlay["camera_name_overlay"]["truncated"])
            self.assertEqual(metadata["cameras"][0]["camera_name"], long_name)
            tile_width = 1920 // int(tile_count ** 0.5)
            for transition in (0.2, 1.2):
                frame = self._video_frame(rendered, transition, 1920, 1080)
                min_channel = 145 if tile_count == 16 else 195
                min_pixels = 2 if tile_count == 16 else 3
                tile_rows = self._bright_text_rows(frame, 1920, 0, tile_width, 0, 250,
                                                   threshold=min_channel, minimum_pixels=min_pixels)
                self.assertEqual(len(tile_rows), 2, f"{tile_count}-tile labels should stay a name/time pair: {tile_rows}")
                if tile_count == 16:
                    dense_label_heights = [bottom - top + 1 for top, bottom in tile_rows]
                    self.assertGreaterEqual(min(dense_label_heights), 5,
                                            f"1080p 16-tile glyphs should remain readable: {dense_label_heights}")
                label_bounds = self._bright_pixel_bounds(frame, 1920, 0, tile_width, 0, 250,
                                                        threshold=min_channel)
                self.assertIsNotNone(label_bounds)
                self.assertLess(label_bounds[2], int(tile_width * 0.78), f"{tile_count}-tile camera labels must remain inside a bounded tile region: {label_bounds}")
                notice_rows = self._bright_text_rows(frame, 1920, 400, 1520, 980, 1080)
                self.assertEqual(len(notice_rows), 1, f"{tile_count}-tile gap notice should use the separate bottom band: {notice_rows}")
                if tile_count == 1 and transition == 0.2:
                    first_global_notice_heights = [bottom - top + 1 for top, bottom in notice_rows]
                if transition == 1.2:
                    after_path = evidence_dir / f"grid-after-1080p-{tile_count}-tile.png"
                    self._save_rgb_png(frame, 1920, 1080, after_path)
                    measured = measure_grid_frame(frame, 1920, 1080, tile_count, metadata, "1080p")
                    measured["artifact"] = after_path.name
                    grid_measurements["current_frames"].append(measured)
                    if tile_count == 4:
                        before_path = evidence_dir / "grid-before-1080p-4-tile.png"
                        comparison_path = evidence_dir / "grid-comparison-1080p-4-tile.png"
                        before_frame = old_style_grid_frame(
                            tile_count, 1920, 1080, overlay["first_source_labels"][1],
                        )
                        self._save_rgb_png(before_frame, 1920, 1080, before_path)
                        subprocess.run([
                            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(before_path),
                            "-i", str(after_path), "-filter_complex", "hstack=inputs=2", "-frames:v", "1",
                            "-compression_level", "9", "-y", str(comparison_path),
                        ], check=True, timeout=30)
                        baseline_rows = self._bright_text_rows(before_frame, 1920, 0, 960, 0, 540)
                        baseline_gap_rows = self._bright_text_rows(before_frame, 1920, 400, 1520, 0, 400)
                        grid_measurements["baseline_comparisons"].append({
                            "resolution": "1080p", "tile_count": 4,
                            "baseline_source": "origin/main app/exports.py at 4f09d5354c57244bb4826d33ec4bed804a35188d",
                            "baseline_style": legacy_style,
                            "baseline_cue_layout": "unbounded per-tile name and timestamp plus stacked global gap interval at top",
                            "before_frame": before_path.name, "after_frame": after_path.name,
                            "comparison_frame": comparison_path.name,
                            "baseline_first_tile_text_rows": [list(row) for row in baseline_rows],
                            "baseline_gap_text_rows": [list(row) for row in baseline_gap_rows],
                        })
            if tile_count == 1:
                first_tile_heights = [bottom - top + 1 for top, bottom in self._bright_text_rows(
                    self._video_frame(rendered, 0.2, 1920, 1080), 1920, 0, 1920, 0, 250,
                )]
                self.assertTrue(all(24 <= height <= 30 for height in first_tile_heights),
                                f"1080p full-frame grid glyphs should be 24–30 px high: {first_tile_heights}")

        four_k_label_heights: list[int] = []
        four_k_notice_heights: list[int] = []
        for tile_count in (1, 4, 9, 16):
            accepted_4k = self.client.post("/api/exports", json={
                "camera_ids": camera_ids[:tile_count], "tile_camera_ids": camera_ids[:tile_count],
                "output_modes": ["grid"], "grid_resolution": "4k",
                "start": "2026-10-20T10:00:00Z", "end": "2026-10-20T10:00:05Z",
            })
            self.assertEqual(accepted_4k.status_code, 202, accepted_4k.text)
            result_4k = self._wait_for_status(accepted_4k.json()["id"], "completed", "failed")
            self.assertEqual(result_4k["status"], "completed", result_4k)
            with zipfile.ZipFile(io.BytesIO(self.client.get(result_4k["download_url"]).content)) as archive:
                metadata_4k = json.loads(archive.read("metadata.json"))
                rendered_4k = Path(self.temporary.name) / f"grid-4k-{tile_count}-tiles.mp4"
                rendered_4k.write_bytes(archive.read("grid.mp4"))
            self.assertEqual(metadata_4k["grid"]["resolution"], "4k")
            self.assertEqual(len(metadata_4k["grid"]["global_gaps_compressed"]), 2)
            overlay_4k = metadata_4k["grid"]["overlays"][self.camera]
            self.assertEqual(overlay_4k["first_source_labels"], ["October 20, 2026 06:00:01", "October 20, 2026 06:00:04"])
            self.assertTrue(overlay_4k["camera_name_overlay"]["truncated"])
            columns = int(tile_count ** 0.5)
            tile_width, tile_height = 3840 // columns, 2160 // columns
            pixel_threshold = 145 if tile_count >= 9 else 195
            minimum_pixels = 2 if tile_count >= 9 else 3
            for transition in (0.2, 1.2):
                frame = self._video_frame(rendered_4k, transition, 3840, 2160)
                tile_rows = self._bright_text_rows(
                    frame, 3840, 0, tile_width, 0, min(tile_height, 500),
                    threshold=pixel_threshold, minimum_pixels=minimum_pixels,
                )
                self.assertEqual(len(tile_rows), 2,
                                 f"4K {tile_count}-tile labels should stay a name/time pair at {transition}s: {tile_rows}")
                label_bounds = self._bright_pixel_bounds(
                    frame, 3840, 0, tile_width, 0, min(tile_height, 500), threshold=pixel_threshold,
                )
                self.assertIsNotNone(label_bounds)
                self.assertLess(label_bounds[2], int(tile_width * 0.78),
                                f"4K {tile_count}-tile label spilled from its tile: {label_bounds}")
                label_heights = [bottom - top + 1 for top, bottom in tile_rows]
                if tile_count == 16:
                    self.assertGreaterEqual(min(label_heights), 10,
                                            f"4K 16-tile glyphs must remain readable: {label_heights}")
                notice_rows = self._bright_text_rows(frame, 3840, 800, 3040, 2050, 2160)
                self.assertEqual(len(notice_rows), 1,
                                 f"4K {tile_count}-tile gap notice should stay in its bottom band at {transition}s: {notice_rows}")
                if transition == 1.2:
                    after_path = evidence_dir / f"grid-after-4k-{tile_count}-tile.png"
                    self._save_rgb_png(frame, 3840, 2160, after_path)
                    measured = measure_grid_frame(frame, 3840, 2160, tile_count, metadata_4k, "4k")
                    measured["artifact"] = after_path.name
                    grid_measurements["current_frames"].append(measured)
                    if tile_count == 16:
                        before_path = evidence_dir / "grid-before-4k-16-tile.png"
                        comparison_path = evidence_dir / "grid-comparison-4k-16-tile.png"
                        before_frame = old_style_grid_frame(
                            tile_count, 3840, 2160, overlay_4k["first_source_labels"][1],
                        )
                        self._save_rgb_png(before_frame, 3840, 2160, before_path)
                        subprocess.run([
                            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(before_path),
                            "-i", str(after_path), "-filter_complex", "hstack=inputs=2", "-frames:v", "1",
                            "-compression_level", "9", "-y", str(comparison_path),
                        ], check=True, timeout=60)
                        baseline_rows = self._bright_text_rows(before_frame, 3840, 0, 480, 0, 540,
                                                               threshold=145, minimum_pixels=2)
                        baseline_gap_rows = self._bright_text_rows(before_frame, 3840, 800, 3040, 0, 400,
                                                                   threshold=145, minimum_pixels=2)
                        grid_measurements["baseline_comparisons"].append({
                            "resolution": "4k", "tile_count": 16,
                            "baseline_source": "origin/main app/exports.py at 4f09d5354c57244bb4826d33ec4bed804a35188d",
                            "baseline_style": legacy_style,
                            "baseline_cue_layout": "unbounded per-tile name and timestamp plus stacked global gap interval at top",
                            "before_frame": before_path.name, "after_frame": after_path.name,
                            "comparison_frame": comparison_path.name,
                            "baseline_first_tile_text_rows": [list(row) for row in baseline_rows],
                            "baseline_gap_text_rows": [list(row) for row in baseline_gap_rows],
                        })
                if tile_count == 1 and transition == 0.2:
                    four_k_label_heights = label_heights
                    four_k_notice_heights = [bottom - top + 1 for top, bottom in notice_rows]
        ratio = max(four_k_label_heights) / max(first_tile_heights)
        self.assertGreater(ratio, 1.7, f"4K text should scale from the 1080p tile size: 1080p={first_tile_heights}, 4K={four_k_label_heights}")
        self.assertLess(ratio, 2.3, f"4K text should scale proportionally instead of growing faster: 1080p={first_tile_heights}, 4K={four_k_label_heights}")
        notice_ratio = max(four_k_notice_heights) / max(first_global_notice_heights)
        self.assertGreater(notice_ratio, 1.7, f"4K global cue should scale from 1080p: notice ratio={notice_ratio}")
        self.assertLess(notice_ratio, 2.3, f"4K global cue should scale proportionally: notice ratio={notice_ratio}")
        self.assertEqual(len(grid_measurements["current_frames"]), 8)
        self.assertEqual(len(grid_measurements["baseline_comparisons"]), 2)
        measurements_path = evidence_dir / "grid-measurements.json"
        measurements_path.write_text(json.dumps(grid_measurements, indent=2) + "\n", encoding="utf-8")
        evidence_size = sum(path.stat().st_size for path in evidence_dir.glob("grid-*.png"))
        self.assertLess(evidence_size, 5 * 1024 * 1024,
                        f"generated grid evidence should stay compact, got {evidence_size} bytes")

    def test_optional_audio_is_muxed_and_grid_audio_stays_silent_over_compressed_missing_source_time(self):
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("FFmpeg and ffprobe are required for generated audio checks")
        second = str(uuid.uuid4())
        audio_source = self.root / self.camera / "audio.mp4"
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10",
                        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000", "-t", "1", "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", str(audio_source)], check=True, timeout=30)
        silent_source = self.root / second / "silent.mp4"
        silent_source.parent.mkdir(parents=True)
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10",
                        "-t", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(silent_source)], check=True, timeout=30)
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,audio_supported,audio_enabled) VALUES(?,?,X'01','disabled',0,0)", (second, "Audio-disabled selected source"))
            db.execute("UPDATE cameras SET audio_supported=1,audio_enabled=1 WHERE id=?", (self.camera,))
            db.execute("UPDATE recording_segments SET relative_path=?,size_bytes=? WHERE id=?", (f"{self.camera}/audio.mp4", audio_source.stat().st_size, self.segment_id))
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                       (second, f"{second}/silent.mp4", "2026-10-20T10:00:02+00:00", "2026-10-20T10:00:03+00:00", silent_source.stat().st_size))
        accepted = self.client.post("/api/exports", json={
            "camera_ids": [self.camera, second], "output_modes": ["individual", "grid"], "audio_camera_id": self.camera,
            "include_audio": True, "start": "2026-10-20T10:00:00Z", "end": "2026-10-20T10:00:03Z",
        })
        self.assertEqual(accepted.status_code, 202, accepted.text)
        result = self._wait_for_status(accepted.json()["id"], "completed", "failed")
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["include_audio"])
        with zipfile.ZipFile(io.BytesIO(self.client.get(result["download_url"]).content)) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            self.assertEqual(metadata["audio"]["grid_camera_id"], self.camera)
            self.assertEqual(len(metadata["audio"]["grid_source_to_output_intervals"]), 1)
            self.assertTrue(metadata["cameras"][0]["audio"]["included"])
            self.assertFalse(metadata["cameras"][1]["audio"]["included"])
            first_individual = Path(self.temporary.name) / "individual-audio.mp4"
            second_individual = Path(self.temporary.name) / "individual-silent.mp4"
            grid = Path(self.temporary.name) / "grid-audio.mp4"
            first_individual.write_bytes(archive.read(metadata["cameras"][0]["video_file"]))
            second_individual.write_bytes(archive.read(metadata["cameras"][1]["video_file"]))
            grid.write_bytes(archive.read("grid.mp4"))
            integrity = self.client.post(f"/api/exports/{result['id']}/verify", content=io.BytesIO(self.client.get(result["download_url"]).content).getvalue(), headers={"content-type": "application/zip"})
            self.assertEqual(integrity.status_code, 200, integrity.text)
            self.assertTrue(integrity.json()["checksums_valid"])
        for media in (first_individual, grid):
            probe = subprocess.run([self.ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "json", str(media)], capture_output=True, check=True, timeout=15)
            self.assertEqual(json.loads(probe.stdout)["streams"][0]["codec_name"], "aac")
        silent_probe = subprocess.run([self.ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "json", str(second_individual)], capture_output=True, check=True, timeout=15)
        self.assertEqual(json.loads(silent_probe.stdout)["streams"], [])
        pcm = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(grid), "-f", "s16le", "-ac", "1", "-ar", "8000", "-"], capture_output=True, check=True, timeout=20).stdout
        samples = memoryview(pcm).cast("h")
        def rms(start: int, end: int) -> float:
            chunk = samples[start * 8000:end * 8000]
            return (sum(sample * sample for sample in chunk) / max(1, len(chunk))) ** 0.5
        self.assertGreater(rms(0, 1), 500, "the selected camera's recorded audio must be present")
        self.assertLess(rms(1, 2), 100, "the compressed union interval without selected-camera audio must stay silent")

    def test_expiry_removes_zip_but_keeps_metadata_and_download_is_gone(self):
        job_id = self._create()
        output = self.root / "exports" / f"{job_id}.zip"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"evidence")
        with connect() as db:
            db.execute("UPDATE exports SET status='completed',metadata_json=?,output_relative_path=?,expires_at=?,completed_at=? WHERE id=?",
                       (json.dumps({"audio": {"included": False}, "expires_at": "2026-01-01T00:00:00+00:00"}), f"exports/{job_id}.zip", "2026-01-01T00:00:00+00:00", "2025-12-25T00:00:00+00:00", job_id))
        expire_exports(datetime.fromisoformat("2026-01-02T00:00:00+00:00"))
        status = self.client.get(f"/api/exports/{job_id}")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["metadata"], {"audio": {"included": False}, "expires_at": "2026-01-01T00:00:00+00:00"})
        self.assertEqual(status.json()["expires_at"], "2026-01-01T00:00:00+00:00")
        self.assertIsNone(status.json()["download_url"])
        self.assertFalse(output.exists())
        download = self.client.get(f"/api/exports/{job_id}/download")
        self.assertEqual(download.status_code, 410)

    def test_audio_is_rejected_unless_source_is_administrator_enabled(self):
        rejected = self.client.post("/api/exports", json={
            "camera_ids": [self.camera], "output_modes": ["grid"], "include_audio": True,
            "audio_camera_id": self.camera, "start": "2026-10-20T09:59:59Z", "end": "2026-10-20T10:00:01Z",
        })
        self.assertEqual(rejected.status_code, 422, rejected.text)
        with connect() as db:
            db.execute("UPDATE cameras SET audio_supported=1,audio_enabled=1 WHERE id=?", (self.camera,))
        accepted = self.client.post("/api/exports", json={
            "camera_ids": [self.camera], "output_modes": ["grid"], "include_audio": True,
            "audio_camera_id": self.camera, "start": "2026-10-20T09:59:59Z", "end": "2026-10-20T10:00:01Z",
        })
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertTrue(accepted.json()["include_audio"])
        self.assertEqual(accepted.json()["audio_camera_id"], self.camera)

    def test_low_free_space_fails_job_and_releases_source_lease(self):
        import shutil as system_shutil
        tiny_disk = system_shutil._ntuple_diskusage(total=1, used=0, free=1)
        with patch("app.exports.shutil.disk_usage", return_value=tiny_disk):
            job_id = self._create()
            failed = self._wait_for_status(job_id, "failed")
        self.assertIn("Insufficient free storage", failed["error"])
        self.assertFalse(playback_is_leased(self.media))

    def test_restart_marks_in_progress_failed_and_preserves_queued_work(self):
        running = create_export_record(self.user_id, [self.camera], datetime.fromisoformat("2026-10-20T09:59:59.500000+00:00"), datetime.fromisoformat("2026-10-20T10:00:00.800000+00:00"), ["individual"])
        queued = create_export_record(self.user_id, [self.camera], datetime.fromisoformat("2026-10-20T09:59:59.500000+00:00"), datetime.fromisoformat("2026-10-20T10:00:00.800000+00:00"), ["individual"])
        with connect() as db:
            db.execute("UPDATE exports SET status='running' WHERE id=?", (running,))
        app.state.exports._progress(running, 43)
        recover_interrupted_exports()
        with connect() as db:
            failed = db.execute("SELECT status,error,progress FROM exports WHERE id=?", (running,)).fetchone()
            queued_row = db.execute("SELECT status FROM exports WHERE id=?", (queued,)).fetchone()
        self.assertEqual(failed["status"], "failed")
        self.assertIn("restart", failed["error"].lower())
        self.assertEqual(failed["progress"], 43)
        self.assertEqual(queued_row["status"], "queued")

    def test_grid_export_decodes_union_timing_frozen_frame_clock_and_overlay_placement(self):
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("FFmpeg and ffprobe are required for the generated grid check")
        second, empty = str(uuid.uuid4()), str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (second, "Export camera!"))
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (empty, "Dark camera"))
        def color_video(path: Path, color: str, seconds: float = 1.0) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:s=96x64:r=10", "-t", str(seconds), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(path)], check=True, timeout=30)
        color_video(self.media, "red")
        late, blue = self.root / self.camera / "late.mp4", self.root / second / "blue.mp4"
        color_video(late, "yellow")
        color_video(blue, "blue", 2.5)
        with connect() as db:
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)", (self.camera, f"{self.camera}/late.mp4", "2026-10-20T10:00:05+00:00", "2026-10-20T10:00:06+00:00", late.stat().st_size))
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)", (second, f"{second}/blue.mp4", "2026-10-20T10:00:00.500000+00:00", "2026-10-20T10:00:03+00:00", blue.stat().st_size))
        accepted = self.client.post("/api/exports", json={
            "camera_ids": [self.camera, second, empty], "tile_camera_ids": [self.camera, second, empty, None],
            "output_modes": ["individual", "grid"], "grid_resolution": "1080p",
            "start": "2026-10-20T10:00:00Z", "end": "2026-10-20T10:00:06Z",
        })
        self.assertEqual(accepted.status_code, 202, accepted.text)
        result = self._wait_for_status(accepted.json()["id"], "completed", "failed")
        self.assertEqual(result["status"], "completed", result)
        with zipfile.ZipFile(io.BytesIO(self.client.get(result["download_url"]).content)) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            self.assertIn("grid.mp4", archive.namelist())
            individual_files = [entry["video_file"] for entry in metadata["cameras"]]
            self.assertEqual(len(individual_files), 2)
            self.assertEqual(len(set(individual_files)), 2)
            self.assertTrue(all(name.startswith("camera-export-camera-") for name in individual_files))
            self.assertTrue(all(name in archive.namelist() for name in individual_files))
            rendered = Path(self.temporary.name) / "grid.mp4"
            rendered.write_bytes(archive.read("grid.mp4"))
        grid = metadata["grid"]
        self.assertEqual(grid["tile_camera_ids"], [self.camera, second, empty, None])
        self.assertAlmostEqual(grid["duration_seconds"], 4.0, delta=0.001)
        self.assertEqual(len(grid["global_gaps_compressed"]), 1)
        self.assertEqual(grid["overlays"][empty]["missing_tile_label_alignment"], "center")
        self.assertEqual(grid["overlays"][empty]["missing_tile_time_labels"], ["Shared source time", "Last frame captured"])
        frozen_cues = grid["overlays"][self.camera]["missing_tile_cues"]
        self.assertTrue(any(cue["shared_source_time"] == "2026-10-20T10:00:02+00:00" and cue["last_frame_capture_time"] == "2026-10-20T10:00:01+00:00" for cue in frozen_cues))
        probe = subprocess.run([self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(rendered)], capture_output=True, check=True, timeout=15)
        self.assertAlmostEqual(float(json.loads(probe.stdout)["format"]["duration"]), 4.0, delta=0.2)
        # Decode one frame during camera A's frozen interval: its image remains
        # red, while shared time (10:00:02) advances past last capture (10:00:01).
        frame = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "2.5", "-i", str(rendered), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True, timeout=20).stdout
        width, height = 1920, 1080
        def pixel(x: int, y: int) -> tuple[int, int, int]:
            index = (y * width + x) * 3
            return tuple(frame[index:index + 3])
        frozen = pixel(720, 405)
        self.assertGreater(frozen[0], frozen[2], "the frozen first tile must retain the previous red frame")
        # Centered unavailable label occupies tile center; top-left camera/source
        # labels are checked on an available frame at 0.2 seconds.
        self.assertGreater(max(pixel(x, y)[1] for x in range(430, 530, 5) for y in range(240, 300, 5)), 100)
        available = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "0.2", "-i", str(rendered), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True, timeout=20).stdout
        frame = available
        self.assertGreater(max(pixel(x, y)[1] for x in range(18, 300, 5) for y in range(12, 100, 5)), 100, "camera and source labels should render at the top-left")
        # Sample the tile background away from the centered subtitle text.
        dark_selected = pixel(20, 560)
        self.assertLess(max(dark_selected), 20, "a selected camera without footage must remain a dark tile")
        dark_blank = pixel(1000, 560)
        self.assertLess(max(dark_blank), 20, "an empty grid position must remain a dark tile")
        before_first = pixel(1000, 20)
        self.assertLess(max(before_first), 20, "a tile must remain dark before its first recording")
        accepted_4k = self.client.post("/api/exports", json={
            "camera_ids": [self.camera, second, empty], "tile_camera_ids": [self.camera, second, empty, None],
            "output_modes": ["grid"], "grid_resolution": "4k",
            "start": "2026-10-20T10:00:00Z", "end": "2026-10-20T10:00:06Z",
        })
        self.assertEqual(accepted_4k.status_code, 202, accepted_4k.text)
        result_4k = self._wait_for_status(accepted_4k.json()["id"], "completed", "failed")
        self.assertEqual(result_4k["status"], "completed", result_4k)
        with zipfile.ZipFile(io.BytesIO(self.client.get(result_4k["download_url"]).content)) as archive:
            four_k = Path(self.temporary.name) / "grid-4k.mp4"
            four_k.write_bytes(archive.read("grid.mp4"))
        dimensions = subprocess.run([self.ffprobe, "-v", "error", "-show_entries", "stream=width,height", "-of", "json", str(four_k)], capture_output=True, check=True, timeout=15)
        self.assertEqual(json.loads(dimensions.stdout)["streams"][0], {"width": 3840, "height": 2160})

    def test_export_cancellation_releases_source_leases(self):
        job_id = self._create()
        cancelled = self.client.post(f"/api/exports/{job_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        result = self._wait_for_status(job_id, "cancelled", "completed", "failed")
        self.assertIn(result["status"], {"cancelled", "completed"})
        self.assertFalse(playback_is_leased(self.media))

    def test_accepted_export_protects_source_during_lease_handoff_and_until_terminal(self):
        with connect() as db:
            # Keep background retention from removing this deliberately old fixture
            # before the export request can reserve it. The test lowers the policy
            # after acceptance to exercise the cleanup/export race itself.
            db.execute(
                "INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,3650)",
                (self.camera,),
            )
            db.execute(
                "UPDATE recording_segments SET starts_at='2020-01-01T10:00:00+00:00',ends_at='2020-01-01T10:00:01+00:00' WHERE id=?",
                (self.segment_id,),
            )
        payload = {
            "camera_ids": [self.camera],
            "start": "2020-01-01T09:59:59.500000Z",
            "end": "2020-01-01T10:00:00.800000Z",
        }
        with patch.object(self.client.app.state.exports, "enqueue"):
            accepted = self.client.post("/api/exports", json=payload)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        job_id = accepted.json()["id"]
        with connect() as db:
            db.execute(
                "UPDATE retention_policy_overrides SET retention_days=1 WHERE scope_type='camera' AND scope_id=?",
                (self.camera,),
            )

        lease_started, allow_lease = threading.Event(), threading.Event()
        cleanup_done = threading.Event()
        cleanup_result: list[dict] = []
        original_acquire = __import__("app.exports", fromlist=["acquire_playback"]).acquire_playback

        def blocked_acquire(path):
            lease_started.set()
            if not allow_lease.wait(5):
                raise TimeoutError("test did not release the lease handoff")
            original_acquire(path)

        manager = ExportManager()
        with patch("app.exports.acquire_playback", side_effect=blocked_acquire):
            executor = threading.Thread(target=lambda: asyncio.run(manager._execute(job_id)))
            executor.start()
            self.assertTrue(lease_started.wait(5), "export did not reach its source lease handoff")

            def run_cleanup():
                cleanup_result.append(cleanup_expired_recordings(datetime.now().astimezone()))
                cleanup_done.set()

            cleaner = threading.Thread(target=run_cleanup)
            cleaner.start()
            self.assertFalse(cleanup_done.wait(0.2), "retention crossed the source selection to lease handoff")
            self.assertTrue(self.media.is_file())
            allow_lease.set()
            cleaner.join(5)
            executor.join(10)
        self.assertFalse(cleaner.is_alive())
        self.assertFalse(executor.is_alive())
        self.assertEqual(cleanup_result[0]["removed_segments"], 0)
        self.assertEqual(cleanup_result[0]["skipped_playback"], 1)
        self.assertEqual(self.client.get(f"/api/exports/{job_id}").json()["status"], "completed")

        cleanup = cleanup_expired_recordings(datetime.now().astimezone())
        self.assertFalse(self.media.exists(), "terminal export should release its persistent source reservation")
        self.assertEqual(cleanup["removed_segments"], 1)

    def test_queue_limit_is_enforced(self):
        for _ in range(4):
            self._create()
        full = self.client.post("/api/exports", json={
            "camera_ids": [self.camera], "start": "2026-10-20T09:59:59Z", "end": "2026-10-20T10:00:02Z",
        })
        self.assertEqual(full.status_code, 429)

    def test_missing_ffmpeg_is_reported_as_a_failed_job(self):
        with patch("app.exports.shutil.which", return_value=None):
            job_id = self._create()
            failed = self._wait_for_status(job_id, "failed")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("FFmpeg is unavailable", failed["error"])

    def test_jobs_are_authenticated_scoped_and_cancelable(self):
        job_id = self._create()
        own = self.client.get(f"/api/exports/{job_id}")
        self.assertEqual(own.status_code, 200)
        app.dependency_overrides[current_user] = lambda: {"id": self.other_user_id, "username": "second", "role": "operator"}
        hidden = self.client.get(f"/api/exports/{job_id}")
        self.assertEqual(hidden.status_code, 404)
        app.dependency_overrides.pop(current_user, None)
        try:
            denied = self.client.get(f"/api/exports/{job_id}")
        finally:
            app.dependency_overrides[current_user] = lambda: {"id": self.user_id, "username": "exporter", "role": "operator"}
        self.assertEqual(denied.status_code, 401)
        cancelled = self.client.post(f"/api/exports/{job_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(self.client.get(f"/api/exports/{job_id}").json()["status"], "cancelled")

    def test_download_requires_owner_or_administrator(self):
        job_id = create_export_record(self.user_id, [self.camera], datetime.fromisoformat("2026-10-20T09:59:59.500000+00:00"), datetime.fromisoformat("2026-10-20T10:00:00.800000+00:00"), ["individual"])
        package = self.root / "exports" / f"{job_id}.zip"
        package.parent.mkdir(parents=True, exist_ok=True)
        package.write_bytes(b"test evidence zip")
        with connect() as db:
            db.execute("UPDATE exports SET status='completed',output_relative_path=? WHERE id=?", (f"exports/{job_id}.zip", job_id))
        app.dependency_overrides[current_user] = lambda: {"id": self.other_user_id, "username": "second", "role": "operator"}
        denied = self.client.get(f"/api/exports/{job_id}/download")
        self.assertEqual(denied.status_code, 404)
        app.dependency_overrides[current_user] = lambda: {"id": self.user_id, "username": "exporter", "role": "operator"}
        allowed = self.client.get(f"/api/exports/{job_id}/download")
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.content, b"test evidence zip")

    def test_restart_marks_interrupted_job_failed_and_preserves_audit(self):
        # Seed the state a crashed process would leave; the restarted app owns recovery.
        job_id = create_export_record(
            self.user_id, [self.camera],
            datetime.fromisoformat("2026-10-20T09:59:59.500000+00:00"),
            datetime.fromisoformat("2026-10-20T10:00:00.800000+00:00"),
        )
        self.context.__exit__(None, None, None)
        with connect() as db:
            db.execute("UPDATE exports SET status='running' WHERE id=?", (job_id,))
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        result = self.client.get(f"/api/exports/{job_id}").json()
        self.assertEqual(result["status"], "failed")
        self.assertIn("restart", result["error"])
        with connect() as db:
            actions = [row[0] for row in db.execute("SELECT action FROM audit_log WHERE resource_id=? ORDER BY id", (job_id,))]
        self.assertEqual(actions, ["export_requested", "export_failed"])

    def test_creation_rejects_empty_footage_and_bounded_window(self):
        no_footage = self.client.post("/api/exports", json={
            "camera_ids": [self.camera], "start": "2026-10-20T11:00:00Z", "end": "2026-10-20T12:00:00Z",
        })
        self.assertEqual(no_footage.status_code, 422)
        too_long = self.client.post("/api/exports", json={
            "camera_ids": [self.camera], "start": "2026-10-20T00:00:00Z", "end": "2026-10-21T01:00:00Z",
        })
        self.assertEqual(too_long.status_code, 422)

    def test_export_output_selection_is_validated_and_persisted(self):
        with patch.object(self.client.app.state.exports, "enqueue"):
            selected = self.client.post("/api/exports", json={
                "camera_ids": [self.camera],
                "start": "2026-10-20T09:59:59.500000Z",
                "end": "2026-10-20T10:00:00.800000Z",
                "output_modes": ["individual"],
            })
            defaulted = self.client.post("/api/exports", json={
                "camera_ids": [self.camera],
                "start": "2026-10-20T09:59:59.500000Z",
                "end": "2026-10-20T10:00:00.800000Z",
            })
        self.assertEqual(selected.status_code, 202, selected.text)
        self.assertEqual(selected.json()["output_modes"], ["individual"])
        self.assertEqual(defaulted.status_code, 202, defaulted.text)
        self.assertEqual(defaulted.json()["output_modes"], ["individual", "grid"])
        invalid = self.client.post("/api/exports", json={
            "camera_ids": [self.camera],
            "start": "2026-10-20T09:59:59.500000Z",
            "end": "2026-10-20T10:00:00.800000Z",
            "output_modes": ["thumbnail"],
        })
        self.assertEqual(invalid.status_code, 422)

    def test_individual_only_layout_excludes_selected_camera_without_footage(self):
        empty_camera = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (empty_camera, "No footage"))
        selected_ids = [self.camera, empty_camera]
        job_camera_ids = [camera_id for camera_id in selected_ids if camera_id == self.camera]
        with patch.object(self.client.app.state.exports, "enqueue"):
            accepted = self.client.post("/api/exports", json={
                "camera_ids": job_camera_ids, "tile_camera_ids": [self.camera, None, None, None],
                "output_modes": ["individual"],
                "start": "2026-10-20T09:59:59.500000Z", "end": "2026-10-20T10:00:00.800000Z",
            })
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json()["camera_ids"], [self.camera])
        self.assertEqual(accepted.json()["tile_camera_ids"], [self.camera, None, None, None])

    def test_export_captures_site_display_preferences_at_submission(self):
        app.dependency_overrides[current_user] = lambda: {
            "id": self.user_id, "username": "exporter", "role": "administrator",
        }
        configured = self.client.put("/api/settings/display", json={
            "time_zone": "America/New_York", "date_format": "day-month", "clock_format": "24",
        })
        self.assertEqual(configured.status_code, 200, configured.text)
        app.dependency_overrides[current_user] = lambda: {
            "id": self.user_id, "username": "exporter", "role": "operator",
        }
        with patch.object(self.client.app.state.exports, "enqueue"):
            accepted = self.client.post("/api/exports", json={
                "camera_ids": [self.camera],
                "start": "2026-10-20T09:59:59.500000Z",
                "end": "2026-10-20T10:00:00.800000Z",
            })
        self.assertEqual(accepted.status_code, 202, accepted.text)
        job_id = accepted.json()["id"]
        app.dependency_overrides[current_user] = lambda: {
            "id": self.user_id, "username": "exporter", "role": "administrator",
        }
        changed = self.client.put("/api/settings/display", json={
            "time_zone": "UTC", "date_format": "friendly", "clock_format": "12",
        })
        self.assertEqual(changed.status_code, 200, changed.text)
        job = self.client.get(f"/api/exports/{job_id}")
        self.assertEqual(job.status_code, 200, job.text)
        self.assertEqual(job.json()["display_settings"], {
            "time_zone": "America/New_York", "date_format": "day-month", "clock_format": "24",
        })

    def test_export_concatenates_unique_coverage_with_source_clock_overlay_and_omits_empty_camera(self):
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("FFmpeg and ffprobe are required for the playable export check")
        second_media = self.root / self.camera / "second.mp4"
        overlap_media = self.root / self.camera / "overlap.mp4"
        subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=10",
            "-t", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(second_media),
        ], check=True, timeout=30)
        subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:r=10",
            "-t", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(overlap_media),
        ], check=True, timeout=30)
        empty_camera = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (empty_camera, "No footage"))
            db.execute(
                "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                (self.camera, f"{self.camera}/second.mp4", "2026-10-20T10:00:02+00:00", "2026-10-20T10:00:03+00:00", second_media.stat().st_size),
            )
            db.execute(
                "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                (self.camera, f"{self.camera}/overlap.mp4", "2026-10-20T10:00:00.500000+00:00", "2026-10-20T10:00:01.500000+00:00", overlap_media.stat().st_size),
            )
        app.dependency_overrides[current_user] = lambda: {"id": self.user_id, "username": "exporter", "role": "administrator"}
        configured = self.client.put("/api/settings/display", json={
            "time_zone": "America/New_York", "date_format": "iso", "clock_format": "24",
        })
        self.assertEqual(configured.status_code, 200, configured.text)
        app.dependency_overrides[current_user] = lambda: {"id": self.user_id, "username": "exporter", "role": "operator"}
        with patch.object(self.client.app.state.exports, "enqueue", wraps=self.client.app.state.exports.enqueue):
            accepted = self.client.post("/api/exports", json={
                "camera_ids": [self.camera, empty_camera],
                "start": "2026-10-20T10:00:00Z", "end": "2026-10-20T10:00:03Z",
                "output_modes": ["individual"],
            })
        self.assertEqual(accepted.status_code, 202, accepted.text)
        result = self._wait_for_status(accepted.json()["id"], "completed", "failed")
        self.assertEqual(result["status"], "completed", result)
        package_response = self.client.get(result["download_url"])
        self.assertEqual(package_response.status_code, 200, package_response.text)
        package = Path(self.temporary.name) / "gapped-export.zip"
        package.write_bytes(package_response.content)
        with zipfile.ZipFile(package) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            video_name = metadata["cameras"][0]["video_file"]
            self.assertIn(video_name, archive.namelist())
            self.assertFalse(any(name.startswith("camera-no-footage-") for name in archive.namelist()))
            video_path = Path(self.temporary.name) / "gapped-camera.mp4"
            video_path.write_bytes(archive.read(video_name))
        self.assertEqual(metadata["display_settings"], {"time_zone": "America/New_York", "date_format": "iso", "clock_format": "24"})
        self.assertEqual(metadata["omitted_cameras"], [{"camera_id": empty_camera, "camera_name": "No footage", "reason": "No indexed footage overlaps the requested interval"}])
        camera = metadata["cameras"][0]
        self.assertEqual(camera["actual_intervals"], [
            {"start": "2026-10-20T10:00:00+00:00", "end": "2026-10-20T10:00:01.500000+00:00"},
            {"start": "2026-10-20T10:00:02+00:00", "end": "2026-10-20T10:00:03+00:00"},
        ])
        self.assertEqual(camera["missing_intervals"], [{"start": "2026-10-20T10:00:01.500000+00:00", "end": "2026-10-20T10:00:02+00:00"}])
        self.assertEqual(camera["source_to_output_intervals"], [
            {"source_start": "2026-10-20T10:00:00+00:00", "source_end": "2026-10-20T10:00:01+00:00", "output_start_seconds": 0.0, "output_end_seconds": 1.0},
            {"source_start": "2026-10-20T10:00:01+00:00", "source_end": "2026-10-20T10:00:01.500000+00:00", "output_start_seconds": 1.0, "output_end_seconds": 1.5},
            {"source_start": "2026-10-20T10:00:02+00:00", "source_end": "2026-10-20T10:00:03+00:00", "output_start_seconds": 1.5, "output_end_seconds": 2.5},
        ])
        self.assertEqual(camera["overlay"]["first_source_labels"], ["2026-10-20 06:00:00", "2026-10-20 06:00:01", "2026-10-20 06:00:02"])
        probe = subprocess.run([self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video_path)], capture_output=True, check=True, timeout=15)
        duration = float(json.loads(probe.stdout)["format"]["duration"])
        self.assertAlmostEqual(duration, 2.5, delta=0.15)
        def center_pixel(at: float) -> tuple[int, int, int]:
            decoded = subprocess.run([
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", str(video_path),
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            ], capture_output=True, check=True, timeout=15).stdout
            offset = (360 * 1280 + 640) * 3
            return tuple(decoded[offset:offset + 3])
        first_color, overlap_color, final_color = center_pixel(0.2), center_pixel(1.2), center_pixel(2.0)
        self.assertLess(max(first_color), 60, "the original recording should appear first")
        self.assertGreater(overlap_color[0], overlap_color[2] * 2, "the unique part of the overlapping recording should follow")
        self.assertGreater(final_color[2], final_color[0] * 2, "the later recording should appear after the source gap")
        ordinary_frame = self._video_frame(video_path, 0.4, 1280, 720)
        gap_frame = self._video_frame(video_path, 1.8, 1280, 720)
        ordinary_rows = self._bright_text_rows(ordinary_frame, 1280, 0, 1280, 0, 220)
        gap_rows = self._bright_text_rows(gap_frame, 1280, 0, 1280, 0, 220)
        gap_notice_rows = self._bright_text_rows(gap_frame, 1280, 0, 1280, 620, 720)
        self.assertEqual(len(ordinary_rows), 2, f"ordinary labels should have a compact name/time pair: {ordinary_rows}")
        self.assertEqual(len(gap_rows), 2, f"gap notices must not add lines beside the camera/time labels: {gap_rows}")
        self.assertEqual(len(gap_notice_rows), 1, f"the concise skipped-gap notice should use a separate bottom band: {gap_notice_rows}")


    def test_evidence_package_hashes_bytes_and_verification_is_independent_of_media_probe(self):
        if not self.ffmpeg:
            self.skipTest("FFmpeg is required to create an evidence export")
        job_id = self._create()
        completed = self._wait_for_status(job_id, "completed", "failed")
        self.assertEqual(completed["status"], "completed", completed)
        payload = self.client.get(completed["download_url"]).content
        response = self.client.post(f"/api/exports/{job_id}/verify", content=payload, headers={"content-type": "application/zip"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["checksums_valid"])
        self.assertEqual(result["media_validation"], "not_performed")
        with zipfile.ZipFile(__import__("io").BytesIO(payload)) as archive:
            names = set(archive.namelist())
            metadata = json.loads(archive.read("metadata.json"))
            integrity = json.loads(archive.read("integrity.json"))
        self.assertIn("integrity.json", names)
        self.assertEqual(metadata["audit_reference"], {"resource_type": "export", "resource_id": job_id})
        self.assertIn("UTC", metadata["timestamp_provenance"])
        self.assertEqual(set(item["path"] for item in integrity["files"]), names - {"integrity.json"})
        self.assertNotIn("stream_url", json.dumps(metadata).lower())
        self.assertNotIn("password", json.dumps(metadata).lower())
        with connect() as db:
            entries = db.execute("SELECT actor_id,action,details_json FROM audit_log WHERE resource_id=? ORDER BY id", (job_id,)).fetchall()
        self.assertEqual(entries[-1]["actor_id"], self.user_id)
        self.assertEqual(entries[-1]["action"], "evidence_verified")
        self.assertEqual(json.loads(entries[-1]["details_json"])["outcome"], "valid")

    def test_evidence_verification_detects_modified_delivered_bytes_and_audits_outcome(self):
        if not self.ffmpeg:
            self.skipTest("FFmpeg is required to create an evidence export")
        job_id = self._create()
        completed = self._wait_for_status(job_id, "completed", "failed")
        self.assertEqual(completed["status"], "completed", completed)
        original = self.client.get(completed["download_url"]).content
        changed = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(changed, "w", compression=zipfile.ZIP_STORED) as altered:
            metadata = json.loads(source.read("metadata.json"))
            video_name = metadata["cameras"][0]["video_file"]
            for name in source.namelist():
                content = source.read(name)
                if name == video_name:
                    content = content[:-1] + bytes([content[-1] ^ 0x01])
                altered.writestr(name, content)
        response = self.client.post(f"/api/exports/{job_id}/verify", content=changed.getvalue(), headers={"content-type": "application/zip"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["checksums_valid"])
        self.assertTrue(any(not item["matches"] for item in response.json()["files"]))
        with connect() as db:
            row = db.execute("SELECT actor_id,action,details_json FROM audit_log WHERE resource_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        self.assertEqual(row["actor_id"], self.user_id)
        self.assertEqual(row["action"], "evidence_verification_failed")
        self.assertEqual(json.loads(row["details_json"])["outcome"], "invalid")

    def test_evidence_verification_is_authenticated_and_scoped_to_export_owner(self):
        job_id = self._create()
        response = self.client.post(f"/api/exports/{job_id}/verify", content=b"", headers={"content-type": "application/zip"})
        self.assertEqual(response.status_code, 409)
        app.dependency_overrides[current_user] = lambda: {"id": self.other_user_id, "username": "second", "role": "operator"}
        denied = self.client.post(f"/api/exports/{job_id}/verify", content=b"", headers={"content-type": "application/zip"})
        self.assertEqual(denied.status_code, 404)
        app.dependency_overrides.pop(current_user, None)
        try:
            unauthenticated = self.client.post(f"/api/exports/{job_id}/verify", content=b"", headers={"content-type": "application/zip"})
        finally:
            app.dependency_overrides[current_user] = lambda: {"id": self.user_id, "username": "exporter", "role": "operator"}
        self.assertEqual(unauthenticated.status_code, 401)


if __name__ == "__main__":
    unittest.main()
