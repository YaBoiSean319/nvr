"""The face watch list (#253, part 2): who is on it, the switches, finding a face in a recording. A stand-in reader and picture source stand in for the models and the footage."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from app import face_models, faces
from app.main import app
from app.principal import current_user
from app.store import connect, initialize
from tests.test_support import principal

AT = "2026-11-06T12:00:00+00:00"


def unit(*values: float) -> np.ndarray:
    return face_models.normalise(np.array(values, dtype=np.float64))


def face(x1: float, y1: float, x2: float, y2: float, embedding: np.ndarray, score: float = 0.95) -> face_models.Face:
    centre = ((x1 + x2) / 2, (y1 + y2) / 2)
    return face_models.Face(score, (x1, y1, x2, y2), ((x1, centre[1]), (x2, centre[1]), centre, (x1, y2), (x2, y2)), embedding, (int((x2 - x1) * 640), int((y2 - y1) * 480)))


class StandInReader:
    def __init__(self, found: list[face_models.Face]):
        self.found = found

    def faces(self, frame: np.ndarray) -> list[face_models.Face]:
        return self.found


class Case(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.directory.name) / "state.sqlite3")
        initialize()
        self.camera = "cam-1"
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(2,'operator','unused','operator')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,X'01')", (self.camera, "Front gate"))
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('cam-2','Back gate',X'01')")
        self.found = [face(0.1, 0.2, 0.3, 0.5, unit(1, 0, 0)), face(0.6, 0.2, 0.7, 0.4, unit(0, 1, 0))]
        self.patches = [patch.object(faces, "reader", lambda: StandInReader(self.found)), patch.object(faces, "frame_at", self.frame)]
        for item in self.patches:
            item.start()
        app.dependency_overrides[current_user] = lambda: principal(1, "admin", "administrator")
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    async def frame(self, camera_id: str, at: datetime) -> np.ndarray:
        frame = np.full((480, 640, 3), 90, dtype=np.uint8)
        frame[100:240, 64:192] = 200  # Something for the entry's picture to show
        return frame

    def tearDown(self):
        self.context.__exit__(None, None, None)
        app.dependency_overrides.pop(current_user, None)
        for item in self.patches:
            item.stop()
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.directory.cleanup()

    def add(self, **extra):
        body = {"camera_id": self.camera, "at": AT, "point": {"x": 0.2, "y": 0.35}, "name": "Visitor A", "reason": "Trespassed 2026-11-01", **extra}
        return self.client.post("/api/faces/watch", json=body)

    def audit_actions(self) -> list[str]:
        with connect() as db:
            return [row[0] for row in db.execute("SELECT action FROM audit_log WHERE resource_type='face_watch' ORDER BY id")]


class FindAndAddTests(Case):
    def test_find_lists_the_faces_in_the_picture_and_is_audited(self):
        body = self.client.post("/api/faces/find", json={"camera_id": self.camera, "at": AT}).json()
        self.assertEqual(len(body["faces"]), 2)
        self.assertEqual((body["faces"][0]["box"]["x"], body["faces"][0]["usable"]), (0.1, True))
        self.assertNotIn("_face", body["faces"][0])
        self.assertEqual(self.audit_actions(), ["face_search_viewed"])

    def test_find_returns_the_picture_to_choose_a_face_on(self):
        import base64
        picture = self.client.post("/api/faces/find", json={"camera_id": self.camera, "at": AT}).json()["picture"]
        self.assertEqual(base64.b64decode(picture)[:2], b"\xff\xd8")

    def test_a_face_chosen_by_point_becomes_an_entry_with_its_numbers_and_a_picture(self):
        response = self.add(expires_at="2026-12-01T00:00:00+00:00")
        self.assertEqual(response.status_code, 201, response.text)
        entry = response.json()
        self.assertEqual((entry["name"], entry["reason"], entry["embeddings"], entry["has_picture"], entry["created_by"], entry["expired"], entry["enabled"]),
                         ("Visitor A", "Trespassed 2026-11-01", 1, True, "admin", False, True))
        self.assertEqual(self.client.get(f"/api/faces/watch/{entry['id']}/picture").content[:2], b"\xff\xd8")
        with connect() as db:
            stored = np.frombuffer(db.execute("SELECT embedding FROM face_embeddings").fetchone()[0], dtype=np.float32)
        np.testing.assert_allclose(stored, [1, 0, 0], atol=1e-6)  # The first face, not the second
        self.assertEqual(self.audit_actions(), ["face_search_viewed", "face_watch_added"] if False else ["face_watch_added"])

    def test_a_face_chosen_by_box_picks_the_best_overlap_and_a_miss_is_refused(self):
        second = self.add(point=None, box={"x": 0.61, "y": 0.21, "width": 0.09, "height": 0.19}, name="Visitor B", reason="Banned")
        self.assertEqual(second.status_code, 201, second.text)
        with connect() as db:
            stored = np.frombuffer(db.execute("SELECT embedding FROM face_embeddings WHERE entry_id=?", (second.json()["id"],)).fetchone()[0], dtype=np.float32)
        np.testing.assert_allclose(stored, [0, 1, 0], atol=1e-6)
        miss = self.add(point=None, box={"x": 0.8, "y": 0.8, "width": 0.1, "height": 0.1})
        self.assertEqual(miss.status_code, 422)
        self.assertIn("No face was found", miss.json()["detail"])
        self.assertEqual(self.add(point={"x": 0.9, "y": 0.9}).status_code, 422)

    def test_another_picture_of_the_same_person_is_added_to_the_entry(self):
        entry = self.add().json()
        again = self.add(entry_id=entry["id"], name=None, reason=None)
        self.assertEqual((again.status_code, again.json()["embeddings"]), (201, 2))
        self.assertEqual(self.add(entry_id="nobody", name=None, reason=None).status_code, 404)
        self.assertEqual(self.audit_actions(), ["face_watch_added", "face_watch_picture_added"])

    def test_a_name_and_a_reason_are_needed_and_the_face_is_chosen_one_way(self):
        self.assertEqual(self.add(name="  ").status_code, 422)
        self.assertEqual(self.add(reason="").status_code, 422)
        self.assertEqual(self.add(box={"x": 0.1, "y": 0.2, "width": 0.2, "height": 0.3}).status_code, 422)  # Both a box and a point
        self.assertEqual(self.add(at="2026-11-06T12:00:00").status_code, 422)  # No timezone
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM face_watch_entries").fetchone()[0], 0)

    def test_a_face_inside_a_privacy_mask_cannot_be_used(self):
        with connect() as db:
            db.execute("INSERT INTO privacy_masks(camera_id,zones_json,updated_at) VALUES(?,?,?)", (self.camera, json.dumps([{"x": 0.05, "y": 0.1, "width": 0.3, "height": 0.5}]), AT))
        found = self.client.post("/api/faces/find", json={"camera_id": self.camera, "at": AT}).json()["faces"]
        # A masked face is not in the answer at all (#301): its box, score and a "this is in a mask" note would each say something is there.
        self.assertEqual([(item["box"]["x"], item["usable"]) for item in found], [(0.6, True)])
        refused = self.add()  # The point is on the masked face
        self.assertEqual(refused.status_code, 422)
        self.assertIn("No face was found", refused.json()["detail"])
        self.assertNotIn("mask", refused.json()["detail"].lower())
        self.assertEqual(self.add(point={"x": 0.65, "y": 0.3}).status_code, 201)  # The other face is fine

    def test_missing_models_and_missing_footage_say_so(self):
        def no_models():
            raise faces.FaceError(503, "The face models are not installed on this server")

        with patch.object(faces, "reader", no_models):
            response = self.client.post("/api/faces/find", json={"camera_id": self.camera, "at": AT})
        self.assertEqual((response.status_code, response.json()["detail"]), (503, "The face models are not installed on this server"))

        async def none(camera_id, at):
            raise faces.FaceError(404, "There is no recording of that camera at that time")

        with patch.object(faces, "frame_at", none):
            self.assertEqual(self.client.post("/api/faces/find", json={"camera_id": self.camera, "at": AT}).status_code, 404)


class ListTests(Case):
    def test_the_list_shows_expiry_and_only_live_entries_are_compared(self):
        self.add(name="Live")
        self.add(name="Soon gone", expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        expired = self.add(name="Expired", expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat()).json()
        off = self.add(name="Switched off").json()
        self.assertTrue(self.client.patch(f"/api/faces/watch/{off['id']}", json={"enabled": False}).json()["enabled"] is False)
        entries = {item["name"]: item for item in self.client.get("/api/faces/watch").json()["entries"]}
        self.assertEqual((entries["Expired"]["expired"], entries["Live"]["expired"], entries["Switched off"]["enabled"]), (True, False, False))
        with connect() as db:
            names = {faces.get_entry(db, entry_id)["name"] for entry_id, _ in faces.watched(db)}
            db.execute("INSERT INTO face_watch_entries(id,name,reason) VALUES('bare','No numbers','x')")  # An entry with no embedding has nothing to compare
            self.assertNotIn("bare", [entry_id for entry_id, _ in faces.watched(db)])
        self.assertEqual(names, {"Live", "Soon gone"})
        self.assertNotIn(expired["id"], names)

    def test_the_nearest_live_entry_is_found_for_a_face(self):
        first, second = self.add().json(), self.add(point={"x": 0.65, "y": 0.3}, name="Visitor B").json()
        with connect() as db:
            watching = faces.watched(db)
        near_first = face_models.match(unit(0.9, 0.1, 0), watching)
        self.assertEqual(near_first[0], first["id"])
        self.assertEqual(face_models.match(unit(0.05, 1, 0), watching)[0], second["id"])
        self.assertIsNone(face_models.match(unit(0, 0, 1), watching))


class ChangeTests(Case):
    def test_an_entry_can_be_changed_and_its_expiry_cleared_with_the_change_recorded(self):
        entry = self.add(expires_at="2030-01-01T00:00:00+00:00").json()
        changed = self.client.patch(f"/api/faces/watch/{entry['id']}", json={"name": "Visitor A (banned)", "reason": " Repeat visit ", "clear_expiry": True}).json()
        self.assertEqual((changed["name"], changed["reason"], changed["expires_at"]), ("Visitor A (banned)", "Repeat visit", None))
        self.assertEqual(self.client.patch(f"/api/faces/watch/{entry['id']}", json={"name": " "}).status_code, 422)
        self.assertEqual(self.client.patch("/api/faces/watch/nobody", json={"enabled": False}).status_code, 404)
        with connect() as db:
            details = json.loads(db.execute("SELECT details_json FROM audit_log WHERE action='face_watch_updated'").fetchone()[0])
        self.assertEqual(sorted(details["changed"]), ["expires_at", "name", "reason"])
        self.assertEqual(details["was"]["name"], "Visitor A")

    def test_removing_an_entry_removes_its_numbers_and_its_picture_and_is_recorded(self):
        entry = self.add().json()
        self.add(entry_id=entry["id"], name=None, reason=None)
        self.assertEqual(self.client.delete(f"/api/faces/watch/{entry['id']}").status_code, 204)
        with connect() as db:
            self.assertEqual((db.execute("SELECT COUNT(*) FROM face_embeddings").fetchone()[0], db.execute("SELECT COUNT(*) FROM face_watch_entries").fetchone()[0]), (0, 0))
        self.assertEqual(self.client.get(f"/api/faces/watch/{entry['id']}/picture").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/faces/watch/{entry['id']}").status_code, 404)
        self.assertEqual(self.audit_actions()[-1], "face_watch_removed")


class SwitchTests(Case):
    def test_every_camera_starts_off_and_each_switch_is_recorded(self):
        listing = self.client.get("/api/faces/cameras").json()
        self.assertEqual([(item["name"], item["enabled"]) for item in listing["cameras"]], [("Back gate", False), ("Front gate", False)])
        self.assertEqual(self.client.put(f"/api/faces/cameras/{self.camera}", json={"enabled": True}).json(), {"camera_id": self.camera, "enabled": True})
        self.assertEqual([item["enabled"] for item in self.client.get("/api/faces/cameras").json()["cameras"]], [False, True])
        self.client.put(f"/api/faces/cameras/{self.camera}", json={"enabled": False})
        self.assertEqual(self.client.put("/api/faces/cameras/nowhere", json={"enabled": True}).status_code, 404)
        self.assertEqual(self.audit_actions(), ["face_camera_switched", "face_camera_switched"])
        with connect() as db:
            self.assertEqual(faces.cameras_enabled(db), [])

    def test_a_deleted_camera_takes_its_switch_with_it(self):
        self.client.put(f"/api/faces/cameras/{self.camera}", json={"enabled": True})
        with connect() as db:
            db.execute("DELETE FROM cameras WHERE id=?", (self.camera,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM face_camera_settings").fetchone()[0], 0)

    def test_the_match_threshold_is_bounded_and_recorded(self):
        status = self.client.get("/api/faces/status").json()
        self.assertEqual((status["match_threshold"], status["cameras_enabled"], status["entries"], status["models"]["available"]), (0.45, 0, 0, False))
        self.assertEqual(self.client.put("/api/faces/settings", json={"match_threshold": 0.6}).json(), {"match_threshold": 0.6})
        self.assertEqual(self.client.get("/api/faces/watch").json()["match_threshold"], 0.6)
        for bad in (0.1, 0.95):
            self.assertEqual(self.client.put("/api/faces/settings", json={"match_threshold": bad}).status_code, 422)
        self.assertEqual(self.audit_actions(), ["face_settings_updated"])


class SightingTests(Case):
    def test_recent_matches_are_listed_with_their_pictures_and_viewing_is_audited(self):
        entry = self.add().json()
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES('t1',?,'person','2026-11-06T12:00:00+00:00','2026-11-06T12:00:00+00:00','m','1','cpu')", (self.camera,))
            for number, seen in (("s1", "2026-11-06T12:00:05+00:00"), ("s2", "2026-11-06T12:30:00+00:00")):
                db.execute("INSERT INTO face_sightings(id,entry_id,camera_id,track_id,seen_at,similarity,box_json,picture) VALUES(?,?,?,?,?,0.8,'{}',?)",
                           (number, entry["id"], self.camera, "t1", seen, b"\xff\xd8jpeg" if number == "s1" else None))
        body = self.client.get("/api/faces/sightings").json()["sightings"]
        self.assertEqual([(item["id"], item["entry_name"], item["camera_name"], item["has_picture"]) for item in body],
                         [("s2", "Visitor A", "Front gate", False), ("s1", "Visitor A", "Front gate", True)])
        self.assertEqual(self.client.get("/api/faces/sightings/s1/picture").content, b"\xff\xd8jpeg")
        self.assertEqual(self.client.get("/api/faces/sightings/s2/picture").status_code, 404)
        self.assertEqual(self.audit_actions()[-2:], ["face_sightings_viewed", "face_sighting_picture_viewed"])


class AccessTests(Case):
    def test_only_administrators_reach_any_of_it(self):
        entry = self.add().json()
        app.dependency_overrides[current_user] = lambda: principal(2, "operator", "operator")
        calls = [("get", "/api/faces/status", None), ("get", "/api/faces/watch", None), ("post", "/api/faces/find", {"camera_id": self.camera, "at": AT}),
                 ("post", "/api/faces/watch", {"camera_id": self.camera, "at": AT, "point": {"x": 0.2, "y": 0.3}, "name": "x", "reason": "y"}),
                 ("patch", f"/api/faces/watch/{entry['id']}", {"enabled": False}), ("delete", f"/api/faces/watch/{entry['id']}", None),
                 ("get", f"/api/faces/watch/{entry['id']}/picture", None), ("get", "/api/faces/cameras", None), ("put", f"/api/faces/cameras/{self.camera}", {"enabled": True}),
                 ("put", "/api/faces/settings", {"match_threshold": 0.5}), ("get", "/api/faces/sightings", None), ("get", "/api/faces/sightings/s1/picture", None)]
        for method, path, body in calls:
            response = getattr(self.client, method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual(response.status_code, 403, (method, path, response.text))
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM face_watch_entries").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
