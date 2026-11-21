"""Face search shows the masked picture, never the original, and stored face pictures obey a mask drawn later (#301). Real authentication and masks; a stand-in
face model and a synthetic recorded picture."""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from app import faces
from app.store import connect
from tests.test_access_grants import GrantCase
from tests.test_faces import AT, StandInReader, face, unit
from tests.test_privacy import brightness

TOP_LEFT = {"x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5}
BOTTOM_RIGHT = {"x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5}


class FacePrivacyCase(GrantCase):
    def setUp(self):
        super().setUp()
        # Faces: A is inside the top-left quarter, B outside it, C reaches into it from the right.
        self.found = [face(0.1, 0.2, 0.3, 0.4, unit(1, 0, 0)), face(0.6, 0.6, 0.8, 0.8, unit(0, 1, 0)), face(0.45, 0.2, 0.6, 0.4, unit(0, 0, 1))]
        self.patches = [patch.object(faces, "reader", lambda: StandInReader(self.found)), patch.object(faces, "frame_at", self.picture)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def picture(self, camera_id: str, at: datetime) -> np.ndarray:
        return np.full((240, 320, 3), 200, dtype=np.uint8)  # The recorded picture: uniformly light, so anything dark in the answer was put there by a mask

    def mask(self, *zones: dict) -> None:
        saved = self.admin.put(f"/api/cameras/{self.d1}/privacy", json={"zones": list(zones)})
        self.assertEqual(saved.status_code, 200, saved.text)

    def find(self, client, **extra):
        return client.post("/api/faces/find", json={"camera_id": self.d1, "at": AT, **extra})

    def luma(self, response) -> tuple[int, int]:
        return brightness(base64.b64decode(response.json()["picture"]), image=True)

    def token_client(self, *scopes: str) -> TestClient:
        secret = self.admin.post("/api/admin/api-tokens", json={"name": "faces", "scopes": list(scopes)}).json()["secret"]
        client = TestClient(self.app)
        client.headers["Authorization"] = f"Bearer {secret}"
        self.addCleanup(client.close)
        return client


class PreviewTests(FacePrivacyCase):
    def test_the_reproduction_a_masked_camera_no_longer_returns_the_original_picture(self):
        before = self.find(self.admin)
        self.assertGreater(min(self.luma(before)), 150, "unmasked, the picture is the original")
        self.mask(TOP_LEFT)
        inside, outside = self.luma(self.find(self.admin))
        self.assertLess(inside, 30, "the masked quarter is black before anything leaves the server")
        self.assertGreater(outside, 150)

    def test_faces_that_touch_a_mask_are_not_in_the_answer_at_all(self):
        self.mask(TOP_LEFT)
        body = self.find(self.admin).json()
        self.assertEqual([item["box"]["x"] for item in body["faces"]], [0.6], "A is inside and C reaches into the mask: neither is listed, not even as unusable")
        text = json.dumps(body)
        for hidden in ("0.1", "0.45"):
            self.assertNotIn(f'"x": {hidden}', text)
        self.assertNotIn("privacy mask", text, "no 'this face is in a mask' either: that says something is there")

    def test_choosing_the_place_of_a_masked_face_finds_nothing_and_says_nothing_about_a_mask(self):
        self.mask(TOP_LEFT)
        refused = self.admin.post("/api/faces/watch", json={"camera_id": self.d1, "at": AT, "point": {"x": 0.2, "y": 0.3}, "name": "X", "reason": "Y"})
        self.assertEqual(refused.status_code, 422)
        self.assertIn("No face was found", refused.json()["detail"])
        self.assertNotIn("mask", refused.json()["detail"].lower())

    def test_an_admin_token_gets_only_the_masked_picture_and_cannot_ask_for_more(self):
        self.mask(TOP_LEFT)
        token = self.token_client("admin")
        for extra in ({}, {"unmasked": True}):
            response = self.find(token, **extra)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertLess(self.luma(response)[0], 30, f"asking for {extra or 'nothing extra'}")
        # A person's own session cannot either: this search has no unmasked mode, whatever grant they hold.
        self.assertEqual(self.admin.post(f"/api/cameras/{self.d1}/privacy/unmasked", json={"reason": "Checking a complaint about the gate"}).status_code, 200)
        self.assertLess(self.luma(self.find(self.admin, unmasked=True))[0], 30)

    def test_an_operator_cannot_search_faces_at_all(self):
        self.assertEqual(self.find(self.limited).status_code, 403)

    def test_changing_the_mask_changes_the_next_answer(self):
        self.mask(TOP_LEFT)
        self.assertEqual([item["box"]["x"] for item in self.find(self.admin).json()["faces"]], [0.6])
        self.mask(BOTTOM_RIGHT)
        body = self.find(self.admin)
        self.assertEqual(sorted(item["box"]["x"] for item in body.json()["faces"]), [0.1, 0.45], "B is now the one behind the mask")
        inside, outside = self.luma(body)
        self.assertGreater(inside, 150)
        self.assertLess(outside, 30)

    def test_a_face_outside_every_mask_can_still_be_enrolled_with_a_picture_that_shows_no_masked_pixels(self):
        self.mask(TOP_LEFT)
        added = self.admin.post("/api/faces/watch", json={"camera_id": self.d1, "at": AT, "point": {"x": 0.7, "y": 0.7}, "name": "Visitor", "reason": "Trespass"})
        self.assertEqual(added.status_code, 201, added.text)
        self.assertEqual(self.admin.get(f"/api/faces/watch/{added.json()['id']}/picture").content[:2], b"\xff\xd8")


class StoredPictureTests(FacePrivacyCase):
    def enrol(self, point: dict) -> str:
        added = self.admin.post("/api/faces/watch", json={"camera_id": self.d1, "at": AT, "point": point, "name": "Visitor", "reason": "Trespass"})
        self.assertEqual(added.status_code, 201, added.text)
        return added.json()["id"]

    def test_an_enrolment_picture_is_hidden_once_a_mask_covers_where_it_was_taken(self):
        entry = self.enrol({"x": 0.7, "y": 0.7})
        self.assertEqual(self.admin.get(f"/api/faces/watch/{entry}/picture").status_code, 200)
        self.mask(BOTTOM_RIGHT)
        self.assertEqual(self.admin.get(f"/api/faces/watch/{entry}/picture").status_code, 404)
        self.mask(TOP_LEFT)
        self.assertEqual(self.admin.get(f"/api/faces/watch/{entry}/picture").status_code, 200, "a mask somewhere else does not hide it")
        self.mask()
        self.assertEqual(self.admin.get(f"/api/faces/watch/{entry}/picture").status_code, 200)

    def test_an_entry_enrolled_before_provenance_was_kept_is_hidden_on_a_masked_camera(self):
        # Where its face was cannot be known, so any mask on its camera may cover it: hidden, as a sighting whose box cannot be compared is.
        # It used to stay visible whatever the masks, the gap left open on #301.
        entry = self.enrol({"x": 0.7, "y": 0.7})
        with connect() as db:
            db.execute("UPDATE face_embeddings SET box_json=NULL")
        self.assertEqual(self.admin.get(f"/api/faces/watch/{entry}/picture").status_code, 200, "no mask, nothing to protect")
        self.mask(TOP_LEFT)
        self.assertEqual(self.admin.get(f"/api/faces/watch/{entry}/picture").status_code, 404)
        self.mask()
        self.assertEqual(self.admin.get(f"/api/faces/watch/{entry}/picture").status_code, 200, "visible again once the mask is gone")

    def sighting(self, entry: str, box: dict, raw_box: str | None = None) -> str:
        track, sighting = str(uuid.uuid4()), str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,detection_count,max_confidence,model_id,model_version,provider) VALUES(?,?,?,?,?,0,0,'m','1','CPU')",
                       (track, self.d1, "person", AT, AT))
            db.execute("INSERT INTO face_sightings(id,entry_id,camera_id,track_id,seen_at,similarity,box_json,picture) VALUES(?,?,?,?,?,?,?,?)",
                       (sighting, entry, self.d1, track, (datetime.fromisoformat(AT) + timedelta(seconds=len(sighting))).isoformat(), 0.9,
                        raw_box if raw_box is not None else json.dumps(box), b"\xff\xd8jpeg"))
        return sighting

    def test_sightings_and_their_pictures_obey_a_mask_drawn_later(self):
        entry = self.enrol({"x": 0.7, "y": 0.7})
        behind = self.sighting(entry, {"x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1})
        clear = self.sighting(entry, {"x": 0.6, "y": 0.6, "width": 0.1, "height": 0.1})
        listed = lambda: sorted(item["id"] for item in self.admin.get("/api/faces/sightings").json()["sightings"])  # noqa: E731
        self.assertEqual(listed(), sorted([behind, clear]))
        self.mask(TOP_LEFT)
        self.assertEqual(listed(), [clear])
        self.assertEqual(self.admin.get(f"/api/faces/sightings/{behind}/picture").status_code, 404)
        self.assertEqual(self.admin.get(f"/api/faces/sightings/{clear}/picture").status_code, 200)

    def test_a_sighting_whose_box_cannot_be_compared_is_hidden_on_a_masked_camera(self):
        entry = self.enrol({"x": 0.7, "y": 0.7})
        broken = self.sighting(entry, {}, raw_box="not json")
        self.assertEqual(self.admin.get(f"/api/faces/sightings/{broken}/picture").status_code, 200, "no mask, nothing to protect")
        self.mask(TOP_LEFT)
        self.assertEqual(self.admin.get(f"/api/faces/sightings/{broken}/picture").status_code, 404)
        self.assertNotIn(broken, [item["id"] for item in self.admin.get("/api/faces/sightings").json()["sightings"]])
