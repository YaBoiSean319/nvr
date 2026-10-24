from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.principal import current_user
from app.store import connect, initialize


class CameraGroupMembershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH")}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.temporary.name) / "recordings")
        initialize()
        self.camera = str(uuid.uuid4())
        self.policy_group = str(uuid.uuid4())
        self.browsing_group = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(2,'operator','unused','operator')")
            db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)", (self.policy_group, "Policy group"))
            db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)", (self.browsing_group, "North wing"))
            db.execute(
                "INSERT INTO cameras(id,name,group_id,stream_url_ciphertext,recording_mode,recording_enabled) VALUES(?,?,?,X'01','motion',1)",
                (self.camera, "Lobby", self.policy_group),
            )
        self.active_user = {"id": 1, "username": "admin", "role": "administrator"}
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

    def test_admin_can_add_and_remove_browsing_membership_without_changing_policy_group(self):
        added = self.client.put(
            f"/api/camera-groups/{self.browsing_group}/members",
            json={"camera_ids": [self.camera, self.camera]},
        )
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(added.json()["camera_ids"], [self.camera])

        groups = self.client.get("/api/camera-groups").json()["groups"]
        by_id = {group["id"]: group for group in groups}
        self.assertEqual(by_id[self.policy_group]["camera_ids"], [self.camera])
        self.assertEqual(by_id[self.browsing_group]["camera_ids"], [self.camera])

        self.assertEqual(self.client.put(
            f"/api/camera-groups/{self.policy_group}/retention", json={"retention_days": 45}
        ).status_code, 200)
        self.assertEqual(self.client.put(
            f"/api/camera-groups/{self.browsing_group}/retention", json={"retention_days": 90}
        ).status_code, 200)
        policy_schedule={"timezone":"UTC","intervals":[{"days":[0,1,2,3,4,5,6],"start":"00:00","end":"23:59","mode":"continuous"}]}
        browse_schedule={"timezone":"UTC","intervals":[{"days":[0,1,2,3,4,5,6],"start":"00:00","end":"23:59","mode":"disabled"}]}
        self.assertEqual(self.client.put(f"/api/camera-groups/{self.policy_group}/schedule", json=policy_schedule).status_code, 200)
        self.assertEqual(self.client.put(f"/api/camera-groups/{self.browsing_group}/schedule", json=browse_schedule).status_code, 200)
        camera=self.client.get("/api/cameras").json()["cameras"][0]
        self.assertEqual(camera["retention_days"],45)
        self.assertEqual(camera["retention_policy_source"],"group")
        self.assertEqual(camera["recording_policy_source"],"group")
        self.assertEqual(camera["effective_recording_mode"],"continuous")

        with connect() as db:
            self.assertEqual(db.execute("SELECT group_id FROM cameras WHERE id=?", (self.camera,)).fetchone()[0], self.policy_group)

        removed = self.client.put(f"/api/camera-groups/{self.browsing_group}/members", json={"camera_ids": []})
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(removed.json()["camera_ids"], [])
        groups = self.client.get("/api/camera-groups").json()["groups"]
        by_id = {group["id"]: group for group in groups}
        self.assertEqual(by_id[self.policy_group]["camera_ids"], [self.camera])
        self.assertEqual(by_id[self.browsing_group]["camera_ids"], [])

    def test_operator_can_list_groups_but_cannot_manage_groups_or_memberships(self):
        self.active_user = {"id": 2, "username": "operator", "role": "operator"}
        self.assertEqual(self.client.get("/api/camera-groups").status_code, 200)
        self.assertEqual(
            self.client.put(f"/api/camera-groups/{self.browsing_group}/members", json={"camera_ids": [self.camera]}).status_code,
            403,
        )
        self.assertEqual(self.client.patch(f"/api/camera-groups/{self.browsing_group}", json={"name": "Renamed"}).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/camera-groups/{self.browsing_group}").status_code, 403)

    def test_group_updates_rename_and_delete_only_unassigned_browsing_groups(self):
        renamed = self.client.patch(f"/api/camera-groups/{self.browsing_group}", json={"name": "East wing"})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["name"], "East wing")
        conflict = self.client.patch(f"/api/camera-groups/{self.browsing_group}", json={"name": "Policy group"})
        self.assertEqual(conflict.status_code, 409, conflict.text)

        blocked = self.client.delete(f"/api/camera-groups/{self.policy_group}")
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(self.client.delete(f"/api/camera-groups/{self.browsing_group}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/camera-groups/{self.browsing_group}/members").status_code, 404)

    def test_membership_rejects_unknown_cameras_and_nonexistent_groups(self):
        unknown = self.client.put(
            f"/api/camera-groups/{self.browsing_group}/members",
            json={"camera_ids": [str(uuid.uuid4())]},
        )
        self.assertEqual(unknown.status_code, 422, unknown.text)
        missing = self.client.put(f"/api/camera-groups/{uuid.uuid4()}/members", json={"camera_ids": [self.camera]})
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_initialize_backfills_existing_policy_group_assignments_as_memberships(self):
        with connect() as db:
            db.execute("DELETE FROM camera_group_memberships")
        initialize()
        groups = self.client.get("/api/camera-groups").json()["groups"]
        by_id = {group["id"]: group for group in groups}
        self.assertEqual(by_id[self.policy_group]["camera_ids"], [self.camera])


if __name__ == "__main__":
    unittest.main()
