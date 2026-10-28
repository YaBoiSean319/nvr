"""Fisheye dewarping (#146): the geometry, its export rendering, lens settings, saved views and exports of a dewarped view."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
import zipfile
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from app import dewarp
from app.main import app
from app.principal import current_user
from app.store import connect, initialize
from tests.fixtures import fisheye_scene
from tests.scene_case import SceneTestCase
from tests.test_support import principal

_frames: dict[str, np.ndarray] = {}


def frame(mount: str = "ceiling") -> np.ndarray:
    """The fixture picture for a mount, made once."""
    if mount not in _frames:
        _frames[mount] = fisheye_scene.fisheye_frame(dewarp.Lens(mount))
    return _frames[mount]


def dark_centres(line: np.ndarray, threshold: float = 100) -> list[float]:
    """Centres of the dark runs along one row or column of grey values; a run cut off by either end of the line is not a whole line and is left out."""
    dark = 255.0 - line
    centres, i = [], 0
    while i < len(dark):
        if dark[i] > threshold:
            j = i
            while j < len(dark) and dark[j] > threshold:
                j += 1
            weights = dark[i:j]
            if i > 0 and j < len(dark):
                centres.append(float((weights * np.arange(i, j)).sum() / weights.sum()))
            i = j
        else:
            i += 1
    return centres


def grey(image: np.ndarray) -> np.ndarray:
    return image.astype(float).mean(axis=2)


def drift(images_lines: list[list[float]], count: int) -> float:
    """The most any line moves between the sampled rows (or columns): 0 for perfectly straight lines. Samples whose line count differs are ignored."""
    usable = [centres for centres in images_lines if len(centres) == count]
    assert len(usable) >= 2, f"lines not found in enough samples: {[len(centres) for centres in images_lines]}"
    return float(max(np.ptp(np.array(usable)[:, k]) for k in range(count)))


class GeometryTests(unittest.TestCase):
    def test_a_panorama_shows_the_rooms_vertical_lines_and_horizontal_bands_straight(self):
        out = grey(dewarp.render(frame(), dewarp.Lens(), dewarp.View("panorama", 0)))
        height, width = out.shape
        wall_rows = [int(height * fraction) for fraction in (0.05, 0.15, 0.25, 0.35, 0.42)]  # Rows that show wall, not the floor, and miss the bands
        columns = [dark_centres(out[row]) for row in wall_rows]
        self.assertEqual(len(columns[0]), 23, columns[0])  # One stripe every 15 degrees all the way round (the two at the strip's ends are the same one, cut by its edges)
        self.assertLess(drift(columns, 23), 1.5)
        # The stripes are where their azimuths put them: 360 degrees across, the first at -180 degrees.
        expected = (np.arange(1, 24) * 15.0 / 360.0) * width
        self.assertLess(np.abs(np.array(columns[0]) - expected).max(), 2.5)
        # The horizontal band at 18.4 degrees below the horizon is one row all the way across.
        band_columns = [int(width * (k + 0.5) / 24) for k in range(0, 24, 3)]
        rows = [dark_centres(out[:int(height * 0.45), column]) for column in band_columns]
        self.assertTrue(all(len(found) >= 1 for found in rows), rows)
        near_band = [min(found, key=lambda value: abs(value - 110.6)) for found in rows]
        self.assertLess(np.ptp(near_band), 1.5)
        self.assertLess(abs(near_band[0] - 110.6), 2.5)

    def test_a_virtual_view_looking_down_shows_the_floor_grid_straight(self):
        out = grey(dewarp.render(frame(), dewarp.Lens(), dewarp.View("view", 0, -90, 90)))
        height, width = out.shape
        vertical = [dark_centres(out[row]) for row in (20, 150, 260, 460, 560, 700)]
        self.assertLess(drift(vertical, len(vertical[0])), 1.5)
        self.assertGreaterEqual(len(vertical[0]), 9)
        spacing = np.diff(vertical[0][1:-1])
        self.assertLess(np.ptp(spacing), 1.5)  # Evenly spaced, as a flat floor seen square on is
        horizontal = [dark_centres(out[:, column]) for column in (60, 190, 320, 450, 580, 700, 830, 960, 1090)]  # Between the vertical lines
        counts = {len(found) for found in horizontal}
        usable = [found for found in horizontal if len(found) == max(counts)]
        self.assertGreaterEqual(len(usable), 3)
        self.assertLess(max(np.ptp(np.array(usable)[:, k]) for k in range(len(usable[0]))), 1.5)

    def test_zooming_a_view_narrows_what_it_shows(self):
        wide = dewarp.render(frame(), dewarp.Lens(), dewarp.View("view", 0, -90, 100))
        narrow = dewarp.render(frame(), dewarp.Lens(), dewarp.View("view", 0, -90, 50))
        self.assertGreater(len(dark_centres(grey(wide)[300])), len(dark_centres(grey(narrow)[300])))

    def test_panning_turns_the_view_the_way_the_azimuth_grows(self):
        left_tint = lambda pan: dewarp.render(frame(), dewarp.Lens(), dewarp.View("view", pan, -25, 60))[360, 640]  # Off the stripes, which sit every 15 degrees
        # The room's wall is tinted by quarter of the azimuth: 0-90 red, 90-180 green, 180-270 blue, 270-360 yellow.
        self.assertTrue(left_tint(52)[0] > 240 and left_tint(52)[1] < 200)  # Red
        self.assertTrue(left_tint(142)[1] > 240 and left_tint(142)[0] < 200)  # Green
        self.assertTrue(left_tint(-128)[2] > 240 and left_tint(-128)[0] < 200)  # Blue
        self.assertTrue(left_tint(-38)[0] > 240 and left_tint(-38)[1] > 240 and left_tint(-38)[2] < 190)  # Yellow

    def test_a_double_panorama_is_two_half_circles_one_above_the_other(self):
        out = dewarp.render(frame(), dewarp.Lens(), dewarp.View("double_panorama", 0))
        self.assertEqual(out.shape[:2], (720, 1280))
        top, bottom = grey(out)[:360], grey(out)[360:]
        # The top strip starts at -180 degrees and the bottom one at 0: the stripes of each sit 15 degrees apart over a half circle.
        top_rows = [dark_centres(top[row]) for row in (20, 70, 120)]
        bottom_rows = [dark_centres(bottom[row]) for row in (20, 70, 120)]
        self.assertEqual((len(top_rows[0]), len(bottom_rows[0])), (11, 11), (top_rows[0], bottom_rows[0]))
        self.assertLess(drift(top_rows, 11), 1.5)
        self.assertLess(drift(bottom_rows, 11), 1.5)
        self.assertGreater(out[100, 100, 2], 240)  # Top strip: blue (180-270 degrees) at the left (it starts at -180 degrees)
        self.assertTrue(out[460, 100, 0] > 240 and out[460, 100, 1] < 200)  # Bottom strip: red (0-90) at the left

    def test_a_wall_mounted_and_a_floor_mounted_lens_dewarp_straight_too(self):
        # Wall: its optical axis is horizontal; the floor grid is below the picture's horizon, the walls' stripes are vertical.
        wall = dewarp.Lens("wall")
        out = grey(dewarp.render(frame("wall"), wall, dewarp.View("view", 0, 0, 70)))  # Level, so the vertical stripes stay parallel
        stripes = [dark_centres(out[row]) for row in (110, 160, 210, 260, 320)]  # Between the bands
        self.assertLess(drift(stripes, len(stripes[0])), 1.5)
        # Floor mount: it looks up at a ceiling grid; looking straight up gives the same straight grid.
        floor = dewarp.Lens("floor")
        up = grey(dewarp.render(frame("floor"), floor, dewarp.View("view", 0, 90, 90)))
        rows = [dark_centres(up[row]) for row in (20, 150, 260, 460, 560, 700)]
        self.assertLess(drift(rows, len(rows[0])), 1.5)

    def test_a_lens_that_is_off_centre_and_smaller_is_followed(self):
        lens = dewarp.Lens("ceiling", 0.4, 0.55, 0.42, 190.0)
        picture = fisheye_scene.fisheye_frame(lens)
        out = grey(dewarp.render(picture, lens, dewarp.View("view", 0, -90, 80)))
        verticals = [dark_centres(out[row]) for row in (30, 200, 500, 690)]
        self.assertLess(drift(verticals, len(verticals[0])), 2.0)

    def test_pan_tilt_and_field_of_view_are_held_to_what_the_mount_allows(self):
        clamped = dewarp.clamp_view("ceiling", dewarp.View("view", 400, 20, 500))
        self.assertEqual((clamped.pan, clamped.tilt, clamped.fov), (180.0, 0.0, dewarp.MAX_VIEW_FOV))
        clamped = dewarp.clamp_view("wall", dewarp.View("view", -170, -120, 1))
        self.assertEqual((clamped.pan, clamped.tilt, clamped.fov), (-90.0, -90.0, dewarp.MIN_VIEW_FOV))


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is needed to check the export rendering")
class ExportRenderingTests(unittest.TestCase):
    def render_with_ffmpeg(self, picture: np.ndarray, lens: dewarp.Lens, view: dewarp.View) -> np.ndarray:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            height, width = picture.shape[:2]
            (root / "f.rgb").write_bytes(picture.tobytes())
            subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-i", str(root / "f.rgb"), "-y", str(root / "f.png")], check=True)
            xmap, ymap, size = dewarp.remap_maps(lens, view, width, height)
            dewarp.write_pgm(root / "x.pgm", xmap)
            dewarp.write_pgm(root / "y.pgm", ymap)
            result = subprocess.run(["ffmpeg", "-v", "error", "-loop", "1", "-i", str(root / "f.png"), "-vf", dewarp.graph(root / "x.pgm", root / "y.pgm", size),
                                     "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
        return np.frombuffer(result.stdout, np.uint8).reshape(size[1], size[0], 3)

    def test_the_exports_rendering_matches_the_reference_geometry_for_every_mode(self):
        lens = dewarp.Lens()
        for view in (dewarp.View("view", 20, -60, 80), dewarp.View("panorama", 30), dewarp.View("double_panorama", 0)):
            exported = self.render_with_ffmpeg(frame(), lens, view)
            reference = dewarp.render(frame(), lens, view)
            difference = np.abs(exported.astype(int) - reference.astype(int))
            self.assertLess(difference.mean(), 6, view)  # Nearest-neighbour samples averaged, against bilinear: the lines' edges differ, the geometry does not
            self.assertLess((difference.max(axis=2) > 64).mean(), 0.03, view)

    def test_the_exported_view_is_straight_where_the_reference_is(self):
        exported = grey(self.render_with_ffmpeg(frame(), dewarp.Lens(), dewarp.View("view", 0, -90, 90)))
        verticals = [dark_centres(exported[row]) for row in (20, 150, 260, 460, 560, 700)]
        self.assertLess(drift(verticals, len(verticals[0])), 2.5)


class LensTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        self.base = f"/api/cameras/{self.camera_id}/lens"

    def operator(self, name: str = "watcher") -> TestClient:
        from app.security import hash_password
        from tests.test_support import TEST_PASSWORD

        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", (name, hash_password(TEST_PASSWORD), "operator"))
        context = TestClient(self.client.app)
        client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.assertEqual(client.post("/api/auth/login", json={"username": name, "password": TEST_PASSWORD}).status_code, 200)
        return client

    def test_administrators_set_a_cameras_lens_and_everyone_who_sees_it_reads_it(self):
        self.assertEqual(self.client.get(self.base).json(), {"kind": "normal"})
        saved = self.client.put(self.base, json={"kind": "fisheye", "mount": "wall", "center_x": 0.48, "center_y": 0.5, "radius": 0.47, "fov": 185})
        self.assertEqual(saved.status_code, 200, saved.text)
        operator = self.operator()
        self.assertEqual(operator.get(self.base).json(), {"kind": "fisheye", "mount": "wall", "center_x": 0.48, "center_y": 0.5, "radius": 0.47, "fov": 185})
        self.assertEqual(operator.get("/api/cameras/lenses").json()["lenses"][self.camera_id]["mount"], "wall")
        self.assertEqual(operator.put(self.base, json={"kind": "normal"}).status_code, 403)  # Only administrators
        with connect() as db:
            audit = db.execute("SELECT details_json FROM audit_log WHERE action='camera_lens_changed'").fetchall()
        self.assertEqual(len(audit), 1)
        # Back to a normal camera: the lens is forgotten.
        self.assertEqual(self.client.put(self.base, json={"kind": "normal"}).status_code, 200)
        self.assertEqual(self.client.get("/api/cameras/lenses").json(), {"lenses": {}})

    def test_values_outside_a_real_lens_are_refused(self):
        for body in ({"kind": "fisheye", "radius": 0.05}, {"kind": "fisheye", "fov": 90}, {"kind": "fisheye", "center_x": 1.5}, {"kind": "fisheye", "mount": "sideways"}):
            self.assertEqual(self.client.put(self.base, json=body).status_code, 422, body)
        self.assertEqual(self.client.put("/api/cameras/nope/lens", json={"kind": "fisheye"}).status_code, 404)

    def test_a_person_who_cannot_see_a_camera_does_not_learn_it_is_a_fisheye(self):
        self.assertEqual(self.client.put(self.base, json={"kind": "fisheye"}).status_code, 200)
        other = self.operator("restricted")
        with connect() as db:
            user = db.execute("SELECT id FROM users WHERE username='restricted'").fetchone()[0]
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('other','Other',X'01')")
        grant = self.client.post("/api/access-grants", json={"user_id": user, "camera_ids": ["other"], "group_ids": []})
        if grant.status_code in (200, 201):
            self.assertNotIn(self.camera_id, other.get("/api/cameras/lenses").json()["lenses"])
            self.assertEqual(other.get(self.base).status_code, 404)

    def layout(self, **extra) -> dict:
        return {"camera_ids": [self.camera_id], "grid_size": 1, "tile_camera_ids": [self.camera_id], "layout_mode": "manual", **extra}

    def test_virtual_views_keep_their_pan_and_zoom_in_saved_views_and_the_workspace(self):
        state = {self.camera_id: {"mode": "views", "pan": 0, "views": [{"pan": 40, "tilt": -35, "fov": 62}, {"pan": -120, "tilt": -10, "fov": 95}]}}
        saved = self.client.put("/api/live/layouts/Lobby", json=self.layout(dewarp=state))
        self.assertEqual(saved.status_code, 200, saved.text)
        shared = self.client.put("/api/live/shared-layouts/Lobby%20shared", json=self.layout(dewarp={self.camera_id: {"mode": "panorama", "pan": 30}}))
        self.assertEqual(shared.status_code, 200, shared.text)
        views = self.client.get("/api/live/views").json()
        [mine] = views["layouts"]
        self.assertEqual(mine["dewarp"][self.camera_id]["views"], [{"pan": 40.0, "tilt": -35.0, "fov": 62.0}, {"pan": -120.0, "tilt": -10.0, "fov": 95.0}])
        self.assertEqual(views["shared_views"][0]["dewarp"][self.camera_id]["mode"], "panorama")
        self.assertEqual(views["shared_views"][0]["dewarp"][self.camera_id]["pan"], 30.0)
        # The workspace remembers it across a reload.
        self.assertEqual(self.client.put("/api/live/workspace", json={**self.layout(dewarp=state), "sidebar_collapsed": False}).status_code, 200)
        restored = self.client.get("/api/live/workspace").json()
        self.assertEqual(restored["dewarp"][self.camera_id]["views"][1]["pan"], -120.0)
        # A layout saved again without it forgets it; raw is not stored at all.
        self.assertEqual(self.client.put("/api/live/layouts/Lobby", json=self.layout(dewarp={self.camera_id: {"mode": "raw"}})).status_code, 200)
        self.assertEqual(self.client.get("/api/live/views").json()["layouts"][0]["dewarp"], {})

    def test_a_dewarped_view_must_belong_to_a_camera_in_the_layout_and_be_in_range(self):
        for state in ({"stranger": {"mode": "panorama"}}, {self.camera_id: {"mode": "views", "views": []}},
                      {self.camera_id: {"mode": "views", "views": [{"pan": 0, "tilt": 0, "fov": 5}]}},
                      {self.camera_id: {"mode": "views", "views": [{"pan": 0, "tilt": -90, "fov": 90}] * 5}}):
            self.assertEqual(self.client.put("/api/live/layouts/Bad", json=self.layout(dewarp=state)).status_code, 422, state)


class DewarpExportTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("FFmpeg is needed for exports")
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_STORAGE_VOLUMES")}
        self.root = Path(self.temporary.name) / "recordings"
        self.root.mkdir()
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root)
        os.environ.pop("NVR_STORAGE_VOLUMES", None)
        initialize()
        self.camera = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'exporter','unused','operator')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (self.camera, "Lobby dome"))
        directory = self.root / self.camera
        directory.mkdir(parents=True)
        picture = frame()
        (directory / "f.rgb").write_bytes(picture.tobytes())
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "1280x720", "-r", "10", "-stream_loop", "-1", "-i", str(directory / "f.rgb"),
                        "-t", "1.5", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(directory / "clip.mp4")], check=True, timeout=60)
        (directory / "f.rgb").unlink()
        with connect() as db:
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                       (self.camera, f"{self.camera}/clip.mp4", "2026-10-20T10:00:00+00:00", "2026-10-20T10:00:01.500000+00:00", (directory / "clip.mp4").stat().st_size))
        app.dependency_overrides[current_user] = lambda: principal(1, "exporter", "operator")
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self):
        if hasattr(self, "context"):
            self.context.__exit__(None, None, None)
            app.dependency_overrides.pop(current_user, None)
            for key, value in self.previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self.temporary.cleanup()

    def set_lens(self, mount: str = "ceiling") -> None:
        with connect() as db:
            db.execute("INSERT OR REPLACE INTO camera_lens(camera_id,mount,center_x,center_y,radius,fov) VALUES(?,?,0.5,0.5,0.5,180)", (self.camera, mount))

    def request(self, **extra):
        return self.client.post("/api/exports", json={"camera_ids": [self.camera], "start": "2026-10-20T10:00:00Z", "end": "2026-10-20T10:00:01.200000Z",
                                                       "output_modes": ["individual"], **extra})

    def wait(self, job_id: str) -> dict:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            result = self.client.get(f"/api/exports/{job_id}").json()
            if result["status"] in {"completed", "failed", "cancelled"}:
                return result
            time.sleep(0.2)
        self.fail("export did not finish")

    def exported_frame(self, job: dict, at: float = 0.4) -> tuple[np.ndarray, dict]:
        download = self.client.get(job["download_url"])
        package = Path(self.temporary.name) / "export.zip"
        package.write_bytes(download.content)
        with zipfile.ZipFile(package) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            name = metadata["cameras"][0]["video_file"]
            video = Path(self.temporary.name) / name
            video.write_bytes(archive.read(name))
        width, height = metadata["cameras"][0]["dewarp"]["output_size"]
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(video), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(height, width, 3), metadata

    def test_an_exported_dewarped_view_matches_the_view_on_screen(self):
        self.set_lens()
        view = {"mode": "view", "pan": 20, "tilt": -60, "fov": 80}
        accepted = self.request(dewarp={self.camera: view})
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json()["dewarp"], {self.camera: {"mode": "view", "pan": 20.0, "tilt": -60.0, "fov": 80.0}})
        job = self.wait(accepted.json()["id"])
        self.assertEqual(job["status"], "completed", job)
        exported, metadata = self.exported_frame(job)
        self.assertEqual(exported.shape, (720, 1280, 3))
        entry = metadata["cameras"][0]["dewarp"]
        self.assertEqual((entry["mode"], entry["pan"], entry["tilt"], entry["fov"], entry["lens"]["mount"]), ("view", 20.0, -60.0, 80.0, "ceiling"))
        reference = dewarp.render(frame(), dewarp.Lens(), dewarp.View("view", 20, -60, 80))
        difference = np.abs(exported.astype(int) - reference.astype(int))
        self.assertLess(difference.mean(), 10)  # Compressed video against the bilinear reference
        self.assertLess((difference.max(axis=2) > 90).mean(), 0.05)

    def test_a_panorama_export_is_straight(self):
        self.set_lens()
        job = self.wait(self.request(dewarp={self.camera: {"mode": "panorama", "pan": 0}}).json()["id"])
        self.assertEqual(job["status"], "completed", job)
        exported, _ = self.exported_frame(job)
        self.assertEqual(exported.shape, (480, 1920, 3))
        out = grey(exported)
        rows = [dark_centres(out[row], 120) for row in (24, 72, 120, 168, 200)]
        count = max(len(found) for found in rows)
        self.assertEqual(count, 23)
        self.assertLess(drift(rows, 23), 3.0)

    def test_the_grid_stays_raw_and_the_request_is_checked(self):
        self.set_lens()
        # A camera without a lens, a camera not in the export, no individual output, and redaction are each refused.
        other = str(uuid.uuid4())
        self.assertEqual(self.request(dewarp={other: {"mode": "view"}}).status_code, 422)
        self.assertEqual(self.client.post("/api/exports", json={"camera_ids": [self.camera], "start": "2026-10-20T10:00:00Z", "end": "2026-10-20T10:00:01Z",
                                                               "output_modes": ["grid"], "dewarp": {self.camera: {"mode": "view"}}}).status_code, 422)
        with connect() as db:
            db.execute("DELETE FROM camera_lens")
        refused = self.request(dewarp={self.camera: {"mode": "view"}})
        self.assertEqual(refused.status_code, 422)
        self.assertIn("fisheye", refused.json()["detail"])
        self.set_lens("wall")
        self.assertEqual(self.request(dewarp={self.camera: {"mode": "double_panorama"}}).status_code, 422)
        self.assertEqual(self.request(dewarp={self.camera: {"mode": "view", "fov": 10}}).status_code, 422)
