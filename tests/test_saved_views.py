from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app, current_user
from app.store import connect, initialize


class SavedViewsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH")}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.temporary.name) / "recordings")
        initialize()
        self.camera_ids = [str(uuid.uuid4()) for _ in range(3)]
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'operator','unused','operator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(2,'other','unused','operator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(3,'admin','unused','administrator')")
            db.executemany(
                "INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')",
                [(camera_id, f"Camera {index}") for index, camera_id in enumerate(self.camera_ids)],
            )
        self.active_user = {"id": 1, "username": "operator", "role": "operator"}
        app.dependency_overrides[current_user] = lambda: self.active_user
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

    def test_personal_views_keep_exact_tiles_and_are_private_to_owner(self):
        camera_a, camera_b, camera_c = self.camera_ids
        response = self.client.put(
            "/api/live/layouts/North%20desk",
            json={"camera_ids": [camera_b, camera_a], "tile_camera_ids": [camera_b, None, camera_a, None], "grid_size": 4, "layout_mode":"auto"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["camera_ids"], [camera_b, camera_a])
        detail = self.client.get("/api/live/views").json()["layouts"][0]
        self.assertEqual(detail["tile_camera_ids"], [camera_b, None, camera_a, None])
        self.assertEqual(detail["grid_size"], 4)
        self.assertEqual(detail["layout_mode"], "auto")

        self.active_user = {"id": 2, "username": "other", "role": "operator"}
        other_views = self.client.get("/api/live/views").json()
        self.assertEqual(other_views["layouts"], [])
        self.assertEqual(other_views["shared_views"], [])

        self.active_user = {"id": 1, "username": "operator", "role": "operator"}
        saved = self.client.get("/api/live/views").json()["layouts"][0]
        self.assertEqual(saved["camera_ids"], [camera_b, camera_a])
        self.assertEqual(saved["tile_camera_ids"], [camera_b, None, camera_a, None])

    def test_admin_can_create_shared_views_that_operators_can_open_but_not_change(self):
        camera_a, camera_b, _ = self.camera_ids
        self.active_user = {"id": 3, "username": "admin", "role": "administrator"}
        created = self.client.put(
            "/api/live/shared-layouts/Front%20desk",
            json={"camera_ids": [camera_a, camera_b], "grid_size": 4, "tile_camera_ids": [camera_b, camera_a, None, None], "layout_mode":"manual"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["scope"], "shared")
        self.assertEqual(self.client.get("/api/live/views").json()["shared_views"][0]["layout_mode"], "manual")

        self.active_user = {"id": 1, "username": "operator", "role": "operator"}
        views = self.client.get("/api/live/views").json()
        self.assertEqual(views["shared_views"][0]["name"], "Front desk")
        self.assertEqual(views["shared_views"][0]["tile_camera_ids"], [camera_b, camera_a, None, None])
        denied_update = self.client.put(
            "/api/live/shared-layouts/Front%20desk", json={"camera_ids": [camera_b]}
        )
        denied_delete = self.client.delete("/api/live/shared-layouts/Front%20desk")
        self.assertEqual(denied_update.status_code, 403)
        self.assertEqual(denied_delete.status_code, 403)

    def test_shared_view_cannot_reference_unknown_cameras_and_admin_can_delete_it(self):
        self.active_user = {"id": 3, "username": "admin", "role": "administrator"}
        invalid = self.client.put(
            "/api/live/shared-layouts/Invalid", json={"camera_ids": [str(uuid.uuid4())], "grid_size": 1}
        )
        self.assertEqual(invalid.status_code, 422)
        created = self.client.put("/api/live/shared-layouts/Good", json={"camera_ids": [self.camera_ids[0]], "grid_size": 1})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(self.client.delete("/api/live/shared-layouts/Good").status_code, 204)

    def test_deleted_camera_remains_in_saved_view_as_an_unavailable_member(self):
        camera_id = self.camera_ids[0]
        response = self.client.put(
            "/api/live/layouts/Desk",
            json={"camera_ids": [camera_id], "grid_size": 1, "tile_camera_ids": [camera_id]},
        )
        self.assertEqual(response.status_code, 200, response.text)

        with connect() as db:
            db.execute("DELETE FROM cameras WHERE id=?", (camera_id,))

        view = self.client.get("/api/live/views").json()["layouts"][0]
        self.assertEqual(view["camera_ids"], [camera_id])
        self.assertEqual(view["tile_camera_ids"], [camera_id])
        self.assertEqual(self.client.get("/api/live/layouts").json()["layouts"], [])

    def test_workspace_round_trips_per_user_layout_and_sidebar_state_without_audio(self):
        camera_a, camera_b, _ = self.camera_ids
        saved = self.client.put(
            "/api/live/workspace",
            json={"camera_ids": [camera_a, camera_b], "tile_camera_ids": [camera_b, None, camera_a, None], "grid_size": 4, "sidebar_collapsed": True},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["sidebar_collapsed"], True)
        self.assertEqual(saved.json()["tile_camera_ids"], [camera_b, None, camera_a, None])
        self.assertEqual(saved.json()["layout_mode"], "auto")
        restored = self.client.get("/api/live/workspace").json()
        self.assertEqual(restored["grid_size"], 4)
        self.assertEqual(restored["layout_mode"], "auto")
        self.assertEqual(restored["camera_ids"], [camera_b, camera_a])
        self.assertNotIn("audio_enabled", restored)

        self.active_user = {"id": 2, "username": "other", "role": "operator"}
        self.assertIsNone(self.client.get("/api/live/workspace").json()["camera_ids"])

    def test_existing_personal_layouts_migrate_to_exact_grid_size_and_tiles(self):
        camera_a, camera_b, _ = self.camera_ids
        with connect() as db:
            db.execute(
                "INSERT INTO viewer_layouts(user_id,name,camera_ids_json) VALUES(1,'Legacy desk',?)",
                (__import__("json").dumps([camera_b, camera_a]),),
            )
        initialize()
        layout = self.client.get("/api/live/views").json()["layouts"][0]
        self.assertEqual(layout["camera_ids"], [camera_b, camera_a])
        self.assertEqual(layout["grid_size"], 4)
        self.assertEqual(layout["layout_mode"], "manual")
        self.assertEqual(layout["tile_camera_ids"][:2], [camera_b, camera_a])
        self.assertEqual(layout["tile_camera_ids"][2:], [None, None])


if __name__ == "__main__":
    unittest.main()
