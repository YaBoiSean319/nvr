"""Burned-in export overlays measured on decoded frames, not on subtitle strings."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from app.application import create_app
from app.principal import current_user
from app.store import connect, initialize

FFMPEG = shutil.which("ffmpeg")
LONG_NAME = "North loading dock exterior camera above the second roll-up door"
TEXT_THRESHOLD = 200


def _row_runs(mask: np.ndarray, join: int = 2) -> list[tuple[int, int]]:
    rows = np.where(mask.any(axis=1))[0]
    runs: list[list[int]] = []
    for row in rows:
        if runs and row - runs[-1][1] <= join:
            runs[-1][1] = row
        else:
            runs.append([row, row])
    return [(start, end) for start, end in runs]


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.where(mask)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _overlaps(a, b) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


@unittest.skipUnless(FFMPEG and shutil.which("ffprobe"), "FFmpeg and ffprobe are required to render and decode exports")
class ExportOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_STORAGE_VOLUMES", "NVR_CREDENTIAL_KEY_PATH")}
        os.environ.update({
            "NVR_DB_PATH": str(self.root / "state.sqlite3"),
            "NVR_RECORDINGS_PATH": str(self.root / "recordings"),
            "NVR_CREDENTIAL_KEY_PATH": str(self.root / "credentials.key"),
        })
        os.environ.pop("NVR_STORAGE_VOLUMES", None)
        (self.root / "recordings").mkdir()
        initialize()
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'investigator','unused','operator')")
        self.clip = self.root / "clip.mp4"
        # A dark scene: only overlay text reaches the brightness threshold.
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x202020:s=640x360:r=15",
                        "-t", "2", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(self.clip)], check=True, timeout=60)
        self.app = create_app()
        self.app.dependency_overrides[current_user] = lambda: {"id": 1, "username": "investigator", "role": "operator"}
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def _camera(self, camera_id: str, name: str, spans: list[tuple[str, str]]) -> None:
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (camera_id, name))
            for index, (start, end) in enumerate(spans):
                relative = f"{camera_id}/{index}.mp4"
                target = self.root / "recordings" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.clip, target)
                db.execute(
                    "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                    (camera_id, relative, f"2026-10-20T{start}+00:00", f"2026-10-20T{end}+00:00", target.stat().st_size),
                )

    def _export(self, camera_ids: list[str], start: str, end: str, **options) -> zipfile.ZipFile:
        response = self.client.post("/api/exports", json={"camera_ids": camera_ids, "start": f"2026-10-20T{start}Z", "end": f"2026-10-20T{end}Z", **options})
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            job = self.client.get(f"/api/exports/{job_id}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.2)
        self.assertEqual(job["status"], "completed", job)
        package = self.root / f"{job_id}.zip"
        package.write_bytes(self.client.get(job["download_url"]).content)
        self.extracted = self.root / job_id
        archive = zipfile.ZipFile(package)
        archive.extractall(self.extracted)
        return archive

    def _frame(self, video: str, seconds: float) -> np.ndarray:
        path = self.extracted / video
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(path)],
                               capture_output=True, check=True, text=True)
        stream = json.loads(probe.stdout)["streams"][0]
        raw = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{seconds:.3f}", "-i", str(path), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(stream["height"], stream["width"])

    def _metadata(self, archive: zipfile.ZipFile) -> dict:
        return json.loads(archive.read("metadata.json"))

    def _label_lines(self, frame: np.ndarray, left: int = 0, top: int = 0, width: int | None = None, height: int | None = None) -> list[int]:
        width = width or frame.shape[1]
        height = height or frame.shape[0]
        region = frame[top:top + height // 3, left:left + width] > TEXT_THRESHOLD
        return [end - start + 1 for start, end in _row_runs(region)]

    def test_individual_and_single_tile_grid_labels_are_compact(self):
        self._camera("cam-1", "Lobby", [("10:00:00", "10:00:02")])
        archive = self._export(["cam-1"], "10:00:00", "10:00:02", grid_resolution="1080p")
        metadata = self._metadata(archive)
        individual = self._frame(metadata["cameras"][0]["video_file"], 1.0)
        self.assertEqual(individual.shape, (720, 1280))
        lines = self._label_lines(individual)
        self.assertEqual(len(lines), 2, lines)
        for line in lines:
            self.assertGreaterEqual(line, 14)
            self.assertLessEqual(line, 21)
        box = _bbox(individual > TEXT_THRESHOLD)
        self.assertLess(box[0], 40)
        self.assertLess(box[1], 40)
        self.assertLess(box[3] - box[1], 0.1 * 720)

        grid = self._frame(metadata["grid"]["video_file"], 1.0)
        self.assertEqual(grid.shape, (1080, 1920))
        for line in self._label_lines(grid):
            self.assertGreaterEqual(line, 22)
            self.assertLessEqual(line, 31)
        self.assertLess(_bbox(grid > TEXT_THRESHOLD)[3], 0.1 * 1080)

    def test_four_k_grid_scales_labels_proportionally(self):
        self._camera("cam-1", "Lobby", [("10:00:00", "10:00:02")])
        archive = self._export(["cam-1"], "10:00:00", "10:00:02", grid_resolution="4k", output_modes=["grid"])
        grid = self._frame(self._metadata(archive)["grid"]["video_file"], 1.0)
        self.assertEqual(grid.shape, (2160, 3840))
        for line in self._label_lines(grid):
            self.assertGreaterEqual(line, 44)
            self.assertLessEqual(line, 62)

    def test_dense_grid_labels_stay_readable_inside_their_tiles(self):
        cameras = [f"cam-{index:02d}" for index in range(16)]
        for index, camera_id in enumerate(cameras):
            self._camera(camera_id, LONG_NAME if index == 5 else f"Camera {index:02d}", [("10:00:00", "10:00:02")])
        archive = self._export(cameras, "10:00:00", "10:00:02", grid_resolution="1080p", output_modes=["grid"])
        grid = self._frame(self._metadata(archive)["grid"]["video_file"], 1.0)
        tile_width, tile_height = 1920 // 4, 1080 // 4
        for index in range(16):
            left, top = index % 4 * tile_width, index // 4 * tile_height
            tile = grid[top:top + tile_height, left:left + tile_width] > TEXT_THRESHOLD
            box = _bbox(tile)
            self.assertIsNotNone(box, f"tile {index} has no label")
            # Inside its own tile, clear of the edges, in the top-left corner.
            self.assertGreater(box[0], 0)
            self.assertLess(box[2], tile_width - 1)
            self.assertLess(box[1], tile_height // 4)
            for line in self._label_lines(grid, left, top, tile_width, tile_height):
                self.assertGreaterEqual(line, 10, f"tile {index} label is unreadably small")
                self.assertLessEqual(line, 16)
        names = [entry["camera_name"] for entry in self._metadata(archive)["grid"]["overlays"].values()]
        self.assertIn(LONG_NAME, names)

    def test_gap_notices_never_overlap_camera_labels(self):
        self._camera("cam-1", LONG_NAME, [("10:00:00", "10:00:02"), ("10:00:10", "10:00:12")])
        archive = self._export(["cam-1"], "09:59:55", "10:00:12", grid_resolution="1080p")
        metadata = self._metadata(archive)
        # The opening skipped interval (output 0s) and the middle gap (output 2s).
        for video, moments in ((metadata["grid"]["video_file"], (0.5, 2.5)), (metadata["cameras"][0]["video_file"], (2.5,))):
            for moment in moments:
                with self.subTest(video=video, moment=moment):
                    frame = self._frame(video, moment)
                    height, width = frame.shape
                    text = frame > TEXT_THRESHOLD
                    label = _bbox(text[: height // 2])
                    notice = _bbox(text[height // 2:])
                    self.assertIsNotNone(label, "camera/time label missing")
                    self.assertIsNotNone(notice, "gap notice missing")
                    notice = (notice[0], notice[1] + height // 2, notice[2], notice[3] + height // 2)
                    self.assertFalse(_overlaps(label, notice), (label, notice))
                    self.assertGreater(notice[1], height * 0.75)
                    centre = (notice[0] + notice[2]) / 2
                    self.assertLess(abs(centre - width / 2), width * 0.1)

    def test_missing_footage_tiles_are_labelled_in_the_centre(self):
        self._camera("cam-1", "Lobby", [("10:00:00", "10:00:02")])
        self._camera("cam-2", "Gate", [])
        archive = self._export(["cam-1", "cam-2"], "10:00:00", "10:00:02", grid_resolution="1080p", output_modes=["grid"])
        grid = self._frame(self._metadata(archive)["grid"]["video_file"], 1.0)
        tile = grid[0:540, 960:1920] > TEXT_THRESHOLD
        box = _bbox(tile)
        self.assertIsNotNone(box)
        centre_x, centre_y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        self.assertLess(abs(centre_x - 480), 60)
        self.assertLess(abs(centre_y - 270), 80)
        labelled = grid[0:540, 0:960] > TEXT_THRESHOLD
        self.assertLess(_bbox(labelled)[1], 60)


if __name__ == "__main__":
    unittest.main()
