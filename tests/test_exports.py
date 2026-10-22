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
        before_first = pixel(1440, 270)
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
        def overlay_bright_pixels(at: float) -> int:
            frame = subprocess.run([
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", str(video_path),
                "-frames:v", "1", "-vf", "crop=1280:220:0:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            ], capture_output=True, check=True, timeout=15).stdout
            return sum(value > 180 for value in frame)
        ordinary_label = overlay_bright_pixels(0.4)
        gap_label = overlay_bright_pixels(1.8)
        self.assertGreater(ordinary_label, 0, "burned source-time label should be visible in the top-left")
        self.assertGreater(gap_label, ordinary_label, "the brief gap indicator should add a visible overlay line")


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
