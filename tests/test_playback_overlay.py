"""Object tracks in Playback (#128): the stored detections served for boxes over video and for a followed object's trail."""
from __future__ import annotations

import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.store import connect
from tests.test_attribute_search import NOW, SearchCase


class OverlayCase(SearchCase):
    def setUp(self):
        super().setUp()
        self.at = NOW - timedelta(minutes=30)
        self.person = self.track(self.d1, "person", [(4, 0.10), (6, 0.22), (8, 0.34), (14, 0.70)])  # Not seen at 10 and 12 s
        self.vehicle = self.track(self.d1, "vehicle", [(4, 0.7), (6, 0.7), (8, 0.7), (10, 0.7), (12, 0.7)], y=0.7)

    def track(self, camera_id: str, label: str, seen: list[tuple[int, float]], y: float = 0.3) -> str:
        track = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,?,?,?,'m','1','CPU')",
                       (track, camera_id, label, (self.at + timedelta(seconds=seen[0][0])).isoformat(), (self.at + timedelta(seconds=seen[-1][0])).isoformat()))
            for seconds, x in seen:
                db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                           "VALUES(?,?,?,?,0.9,?,?,0.1,0.3,'m','1','CPU')", (camera_id, track, (self.at + timedelta(seconds=seconds)).isoformat(), label, x, y))
        return track

    def window(self, seconds_from: int = 0, seconds_to: int = 60) -> dict[str, str]:
        return {"start": (self.at + timedelta(seconds=seconds_from)).isoformat(), "end": (self.at + timedelta(seconds=seconds_to)).isoformat()}

    def overlay(self, camera_id: str | None = None, client: TestClient | None = None, **params):
        return (client or self.admin).get("/api/playback/overlay", params={"camera_id": camera_id or self.d1, **self.window(), **params})

    def mask(self, camera_id: str, zones: list[dict[str, float]]) -> None:
        with connect() as db:
            db.execute("INSERT INTO privacy_masks(camera_id,zones_json,camera_side,updated_at) VALUES(?,?, 'none', ?)", (camera_id, json.dumps(zones), NOW.isoformat()))


class OverlayFrameTests(OverlayCase):
    def test_each_analysed_frame_lists_its_boxes_in_time_order(self):
        body = self.overlay().json()
        self.assertEqual([frame["at"] for frame in body["frames"]], [(self.at + timedelta(seconds=s)).isoformat() for s in (4, 6, 8, 10, 12, 14)])
        by_second = {round((datetime.fromisoformat(frame["at"]) - self.at).total_seconds()): frame for frame in body["frames"]}
        self.assertEqual({box["track_id"] for box in by_second[4]["boxes"]}, {self.person, self.vehicle})
        self.assertEqual([box["track_id"] for box in by_second[10]["boxes"]], [self.vehicle])  # The person is hidden for this frame
        person = next(box for box in by_second[6]["boxes"] if box["track_id"] == self.person)
        self.assertEqual((person["label"], person["box"]["x"], person["box"]["width"]), ("person", 0.22, 0.1))
        self.assertEqual((body["withheld"], body["truncated"]), (0, False))
        self.assertIn("unknown, not empty", body["coverage_note"])

    def test_only_the_asked_stretch_and_camera_are_returned(self):
        self.track(self.d2, "person", [(5, 0.5)])
        body = self.overlay(**self.window(5, 9)).json()
        self.assertEqual(len(body["frames"]), 2)  # 6 and 8 s
        self.assertEqual({box["track_id"] for frame in body["frames"] for box in frame["boxes"]}, {self.person, self.vehicle})
        self.assertEqual(self.overlay(self.d2).json()["frames"][0]["boxes"][0]["label"], "person")
        self.assertEqual(self.overlay(**self.window(100, 160)).json()["frames"], [])

    def test_unanalysed_time_in_the_stretch_is_listed(self):
        self.add_coverage(self.d1, "failed", self.at, self.at + timedelta(seconds=5), "Detector stopped")
        body = self.overlay().json()
        self.assertEqual([item["detail"] for item in body["unanalyzed"]], ["Detector stopped"])

    def test_bad_requests_are_refused(self):
        self.assertEqual(self.overlay(start="yesterday").status_code, 422)
        self.assertEqual(self.overlay(**self.window(60, 0)).status_code, 422)
        self.assertEqual(self.overlay(**self.window(0, 601)).status_code, 422)
        self.assertEqual(self.overlay(**self.window(0, 600)).status_code, 200)
        self.assertEqual(self.overlay(str(uuid.uuid4())).status_code, 404)

    def test_signing_in_is_required(self):
        with TestClient(self.app) as anonymous:
            self.assertEqual(anonymous.get("/api/playback/overlay", params={"camera_id": self.d1, **self.window()}).status_code, 401)
            self.assertEqual(anonymous.get(f"/api/playback/overlay/tracks/{self.person}").status_code, 401)


class OverlayTrackTests(OverlayCase):
    def test_a_followed_object_has_its_whole_stored_path_across_a_pause(self):
        body = self.admin.get(f"/api/playback/overlay/tracks/{self.person}").json()
        seconds = [round((datetime.fromisoformat(point["at"]) - self.at).total_seconds()) for point in body["points"]]
        self.assertEqual(seconds, [4, 6, 8, 14])
        self.assertEqual([point["box"]["x"] for point in body["points"]], [0.10, 0.22, 0.34, 0.70])
        self.assertEqual((body["track_id"], body["camera_id"], body["label"], body["withheld"]), (self.person, self.d1, "person", 0))

    def test_no_answer_carries_an_embedding_or_the_model(self):
        text = self.overlay().text + self.admin.get(f"/api/playback/overlay/tracks/{self.person}").text
        for word in ("vector", "embedding", "model_id", "model_version"):
            self.assertNotIn(word, text)

    def test_an_unknown_track_is_not_found(self):
        self.assertEqual(self.admin.get(f"/api/playback/overlay/tracks/{uuid.uuid4()}").status_code, 404)


class OverlayAccessTests(OverlayCase):
    def setUp(self):
        super().setUp()
        self.yard_track = self.track(self.y1, "person", [(5, 0.5)])

    def test_someone_limited_to_the_dock_cannot_tell_a_hidden_camera_or_track_from_a_missing_one(self):
        self.assertEqual(self.overlay(client=self.limited).status_code, 200)
        hidden = self.overlay(self.y1, client=self.limited)
        missing = self.overlay(str(uuid.uuid4()), client=self.limited)
        self.assertEqual((hidden.status_code, hidden.json()), (404, missing.json()))
        hidden_track = self.limited.get(f"/api/playback/overlay/tracks/{self.yard_track}")
        missing_track = self.limited.get(f"/api/playback/overlay/tracks/{uuid.uuid4()}")
        self.assertEqual((hidden_track.status_code, hidden_track.json()), (404, missing_track.json()))
        self.assertEqual(self.admin.get(f"/api/playback/overlay/tracks/{self.yard_track}").status_code, 200)


class OverlayMaskTests(OverlayCase):
    def test_boxes_that_touch_a_privacy_masked_area_are_never_returned(self):
        self.mask(self.d1, [{"x": 0.0, "y": 0.0, "width": 0.3, "height": 1.0}])  # The left of the picture
        body = self.overlay().json()
        people = [box for frame in body["frames"] for box in frame["boxes"] if box["track_id"] == self.person]
        self.assertEqual([box["box"]["x"] for box in people], [0.34, 0.70])  # 0.10 and 0.22 (to 0.32) touch the mask
        self.assertEqual(body["withheld"], 2)
        points = self.admin.get(f"/api/playback/overlay/tracks/{self.person}").json()
        self.assertEqual([point["box"]["x"] for point in points["points"]], [0.34, 0.70])
        self.assertEqual(points["withheld"], 2)

    def test_the_vehicle_away_from_the_mask_is_unaffected_and_a_frame_with_only_masked_boxes_is_left_out(self):
        self.mask(self.d1, [{"x": 0.0, "y": 0.0, "width": 0.3, "height": 1.0}])
        body = self.overlay().json()
        self.assertEqual(len([box for frame in body["frames"] for box in frame["boxes"] if box["track_id"] == self.vehicle]), 5)
        single = self.track(self.d1, "person", [(30, 0.05)])
        again = self.overlay().json()
        self.assertNotIn((self.at + timedelta(seconds=30)).isoformat(), [frame["at"] for frame in again["frames"]])
        self.assertEqual(again["withheld"], 3)
        self.assertTrue(single)

    def test_unmasked_boxes_need_an_administrators_grant(self):
        self.mask(self.d1, [{"x": 0.0, "y": 0.0, "width": 0.3, "height": 1.0}])
        self.assertEqual(self.overlay(unmasked="true").status_code, 403)  # An administrator without a grant
        self.assertEqual(self.overlay(client=self.full, unmasked="true").status_code, 403)  # An operator
        self.assertEqual(self.overlay(unmasked="false").json()["withheld"], 2)


if __name__ == "__main__":
    unittest.main()
