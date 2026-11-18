"""Privacy masks hide what the AI saw behind them from the structured endpoints as well as from the overlay (#296)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from app.attributes import zone_cells
from app.store import connect
from tests.test_privacy import PrivacyCase

BASE = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=30)
INSIDE = {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}      # entirely inside the masked top-left quarter
OUTSIDE = {"x": 0.6, "y": 0.6, "width": 0.2, "height": 0.2}     # entirely outside it
EDGE = {"x": 0.4, "y": 0.4, "width": 0.3, "height": 0.3}        # reaches into it: touching a mask counts
OTHER_MASK = {"x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5}  # the bottom-right quarter, for a mask that changes


class TrackPrivacyCase(PrivacyCase):
    def add_track(self, boxes: list[dict], *, colour: str = "red", label: str = "person", offset: int = 0) -> tuple[str, list[int]]:
        """A stored track with one detection per box, a second apart, as the AI worker leaves one."""
        track, ids = str(uuid.uuid4()), []
        times = [BASE + timedelta(seconds=offset + index) for index in range(len(boxes))]
        grid = 0
        for box in boxes:
            grid |= zone_cells(box)  # The grid of cells the track passed through, which the tracker fills in and a search by zone starts from
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,detection_count,max_confidence,model_id,model_version,provider,upper_colour,zone_mask) "
                       "VALUES(?,?,?,?,?,0,0,'m','1','CPU',?,?)", (track, self.camera_id, label, times[0].isoformat(), times[-1].isoformat(), colour, grid))  # A trigger keeps the count and times from the detections
            for box, at in zip(boxes, times):
                ids.append(db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,attributes_json,model_id,model_version,provider) "
                                      "VALUES(?,?,?,?,?,?,?,?,?,'{}','m','1','CPU')", (self.camera_id, track, at.isoformat(), label, 0.9, box["x"], box["y"], box["width"], box["height"])).lastrowid)
        return track, ids

    @property
    def window(self) -> dict[str, str]:
        return {"camera_id": self.camera_id, "start": (BASE - timedelta(minutes=1)).isoformat(), "end": (BASE + timedelta(minutes=5)).isoformat()}

    def raw_detections(self, client=None) -> list[dict]:
        response = (client or self.client).get("/api/ai/detections", params=self.window)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["detections"]

    def raw_tracks(self, client=None) -> list[dict]:
        response = (client or self.client).get("/api/ai/tracks", params=self.window)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["tracks"]

    def overlay_boxes(self, client=None) -> list[dict]:
        response = (client or self.client).get("/api/playback/overlay", params=self.window)
        self.assertEqual(response.status_code, 200, response.text)
        return [box for frame in response.json()["frames"] for box in frame["boxes"]]

    def search(self, client=None, **extra) -> list[dict]:
        params = {"start": (BASE - timedelta(minutes=1)).isoformat(), "end": (BASE + timedelta(minutes=5)).isoformat(), "camera_ids": [self.camera_id], **extra}
        response = (client or self.client).get("/api/ai/search", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["results"]


class RawEndpointTests(TrackPrivacyCase):
    def test_the_reproduction_overlay_hides_a_fully_masked_detection_and_so_do_the_raw_endpoints(self):
        track, _ = self.add_track([INSIDE])
        self.mask([{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}])  # The ticket's case: a full-frame mask
        self.assertEqual(self.overlay_boxes(), [])
        self.assertEqual(self.raw_detections(), [])
        self.assertEqual(self.raw_tracks(), [])
        self.assertEqual(self.client.get(f"/api/ai/tracks/{track}").status_code, 404)

    def test_what_the_overlay_shows_the_raw_endpoints_show_and_nothing_more(self):
        visible, _ = self.add_track([OUTSIDE])
        hidden, _ = self.add_track([INSIDE], offset=10)
        edge, _ = self.add_track([EDGE], offset=20)
        self.mask()
        self.assertEqual([item["track_id"] for item in self.raw_detections()], [visible])
        self.assertEqual([item["id"] for item in self.raw_tracks()], [visible])
        self.assertEqual(len(self.overlay_boxes()), 1)
        self.assertEqual(self.client.get(f"/api/ai/tracks/{visible}").status_code, 200)
        for gone in (hidden, edge):
            self.assertEqual(self.client.get(f"/api/ai/tracks/{gone}").status_code, 404, "a box that reaches into a mask is hidden, as in the overlay")

    def test_an_unmasked_camera_is_unchanged(self):
        track, ids = self.add_track([INSIDE, OUTSIDE, EDGE])
        self.assertEqual(len(self.raw_detections()), 3)
        detail = self.client.get(f"/api/ai/tracks/{track}").json()
        self.assertEqual((detail["detection_count"], len(detail["detections"]), detail["attributes"]), (3, 3, {"upper_colour": "red"}))

    def test_a_partial_track_shows_only_what_is_permitted_and_not_how_much_was_hidden(self):
        track, ids = self.add_track([OUTSIDE, INSIDE, INSIDE, OUTSIDE])
        self.mask()
        detail = self.client.get(f"/api/ai/tracks/{track}").json()
        self.assertEqual([item["id"] for item in detail["detections"]], [ids[0], ids[3]])
        self.assertEqual(detail["detection_count"], 2, "the count must not give away the two hidden observations")
        self.assertEqual((detail["first_seen_at"], detail["last_seen_at"]), ((BASE).isoformat(), (BASE + timedelta(seconds=3)).isoformat()))
        self.assertEqual(detail["attributes"], {}, "attributes may have come from the frames the mask hides")
        listed = self.raw_tracks()[0]
        self.assertEqual((listed["detection_count"], listed["attributes"]), (2, {}))
        self.assertEqual([item["id"] for item in self.raw_detections()], [ids[0], ids[3]])

    def test_a_partial_tracks_times_do_not_reveal_a_hidden_stretch(self):
        track, _ = self.add_track([INSIDE, INSIDE, OUTSIDE])  # Starts hidden: first seen, as far as anyone may know, is the third second
        self.mask()
        listed = self.raw_tracks()[0]
        self.assertEqual(listed["first_seen_at"], (BASE + timedelta(seconds=2)).isoformat())

    def test_changing_the_mask_changes_what_later_responses_show(self):
        track, _ = self.add_track([OUTSIDE])
        self.mask()
        self.assertEqual(len(self.raw_tracks()), 1)
        self.mask([OTHER_MASK])  # Now the area the track is in is private
        self.assertEqual(self.raw_tracks(), [])
        self.assertEqual(self.client.get(f"/api/ai/tracks/{track}").status_code, 404)
        self.mask([{"x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1}])  # A small corner nobody is in
        self.assertEqual(len(self.raw_tracks()), 1)

    def test_an_operator_gets_the_same_masked_view(self):
        self.add_track([INSIDE])
        visible, _ = self.add_track([OUTSIDE], offset=10)
        self.mask()
        operator = self.operator()
        self.assertEqual([item["id"] for item in self.raw_tracks(operator)], [visible])
        self.assertEqual([item["track_id"] for item in self.raw_detections(operator)], [visible])

    def test_the_limit_and_truncation_do_not_depend_on_what_was_hidden(self):
        for index in range(6):
            self.add_track([INSIDE], offset=index * 2)
        kept = [self.add_track([OUTSIDE], offset=20 + index * 2)[0] for index in range(2)]
        self.mask()
        response = self.client.get("/api/ai/tracks", params={**self.window, "limit": 2})
        self.assertEqual(([item["id"] for item in response.json()["tracks"]], response.json()["truncated"]), (kept, True), "two visible tracks fill a limit of two, whatever was hidden before them")
        response = self.client.get("/api/ai/tracks", params={**self.window, "limit": 3})
        self.assertEqual((len(response.json()["tracks"]), response.json()["truncated"]), (2, False), "no hidden rows were counted into a full page")


class SearchTests(TrackPrivacyCase):
    def test_search_leaves_out_fully_masked_tracks_and_reduces_partial_ones(self):
        hidden, _ = self.add_track([INSIDE], offset=0)
        partial, _ = self.add_track([OUTSIDE, INSIDE, OUTSIDE], offset=10, colour="blue")
        whole, _ = self.add_track([OUTSIDE], offset=20, colour="green")
        self.mask()
        found = {item["track_id"]: item for item in self.search(label="person")}
        self.assertEqual(set(found), {partial, whole})
        self.assertEqual((found[partial]["detection_count"], found[partial]["attributes"]), (2, {}))
        self.assertEqual(found[partial]["box"], OUTSIDE)
        self.assertEqual(found[whole]["attributes"], {"upper_colour": "green"})

    def test_searching_by_colour_cannot_find_a_track_that_touches_a_mask(self):
        partial, _ = self.add_track([OUTSIDE, INSIDE], offset=0, colour="red")
        whole, _ = self.add_track([OUTSIDE], offset=10, colour="red")
        self.mask()
        self.assertEqual([item["track_id"] for item in self.search(upper_colour="red")], [whole], "a colour match on a partial track would say what is behind the mask")

    def test_a_search_zone_drawn_over_the_mask_finds_nothing_hidden(self):
        self.add_track([INSIDE, INSIDE], offset=0)
        self.mask()
        self.assertEqual(self.search(zone="0,0,0.5,0.5"), [])


class AppearanceSearchTests(TrackPrivacyCase):
    """Ranked by what an object looks like, which comes from the unmasked picture: a track that touches a mask is not a candidate."""

    def embed(self, track: str, values: list[int]) -> None:
        vector = bytes(value & 0xFF for value in values)
        with connect() as db:
            db.execute("INSERT INTO track_embeddings(track_id,model_id,dim,vector,created_at) VALUES(?,?,?,?,?)", (track, "m", len(values), vector, BASE.isoformat()))

    def test_natural_language_search_does_not_rank_a_track_that_touches_a_mask(self):
        partial, _ = self.add_track([OUTSIDE, INSIDE], offset=0)
        hidden, _ = self.add_track([INSIDE], offset=10)
        whole, _ = self.add_track([OUTSIDE], offset=20)
        self.mask()
        params = {"q": "person", "start": (BASE - timedelta(minutes=1)).isoformat(), "end": (BASE + timedelta(minutes=5)).isoformat(), "camera_ids": [self.camera_id]}
        response = self.client.get("/api/ai/search/ask", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual([item["track_id"] for item in body["results"]], [whole])
        self.assertEqual(body["candidates"], 1, "the number of candidates must not count what was hidden")

    def test_find_similar_leaves_out_tracks_that_touch_a_mask_and_refuses_one_as_the_source(self):
        source, _ = self.add_track([OUTSIDE], offset=0)
        like, _ = self.add_track([OUTSIDE], offset=10)
        partial, _ = self.add_track([OUTSIDE, INSIDE], offset=20)
        for track in (source, like, partial):
            self.embed(track, [10, 20, 30, 40])
        self.mask()
        window = {"start": (BASE - timedelta(minutes=1)).isoformat(), "end": (BASE + timedelta(minutes=5)).isoformat()}
        found = self.client.get(f"/api/ai/tracks/{source}/similar", params={**window, "min_similarity": 0.5})
        self.assertEqual(found.status_code, 200, found.text)
        self.assertEqual([item["track_id"] for item in found.json()["results"]], [like])
        # The partial track's own appearance is the unmasked frame: it cannot be the source either.
        self.assertEqual(self.client.get(f"/api/ai/tracks/{partial}/similar", params=window).status_code, 404)


class AggregateTests(TrackPrivacyCase):
    """Counts, maps and the timeline are made from the same detections: what a mask hides is not in them either."""

    def insights_range(self) -> dict[str, str]:
        return {"start": (BASE - timedelta(hours=1)).isoformat(), "end": (BASE + timedelta(hours=1)).isoformat()}

    def people_and_vehicles_counted(self) -> int:
        response = self.client.get("/api/insights", params={**self.insights_range(), "camera_ids": [self.camera_id], "unit": "day"})
        self.assertEqual(response.status_code, 200, response.text)
        return sum(sum(bucket) for bucket in response.json()["counts"][f"camera:{self.camera_id}"])

    def test_the_counts_leave_out_activity_that_was_only_behind_a_mask(self):
        self.add_track([INSIDE], offset=0)
        self.add_track([OUTSIDE], offset=10)
        self.add_track([OUTSIDE, INSIDE], offset=20)
        self.assertEqual(self.people_and_vehicles_counted(), 3)
        self.mask()
        self.assertEqual(self.people_and_vehicles_counted(), 2, "the track seen only behind the mask is not counted; the partial one still is, once")
        self.mask([{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}])
        self.assertEqual(self.people_and_vehicles_counted(), 0)

    def test_the_heatmap_has_no_heat_where_the_mask_is(self):
        self.add_track([INSIDE, INSIDE, INSIDE])
        self.add_track([OUTSIDE], offset=10)
        before = self.client.get("/api/insights/heatmap", params={"camera_id": self.camera_id, **self.insights_range()}).json()
        self.assertEqual(sum(map(sum, before["cells"])), 4, "unmasked, all four samples are on the map")
        self.mask()
        after = self.client.get("/api/insights/heatmap", params={"camera_id": self.camera_id, **self.insights_range()}).json()
        self.assertEqual((sum(map(sum, after["cells"])), after["tracks"]), (1, 1))
        columns, rows = after["columns"], after["rows"]
        masked = [after["cells"][row][column] for row in range(rows // 2) for column in range(columns // 2)]
        self.assertEqual(sum(masked), 0, "the masked quarter of the map is empty")

    def test_the_ai_events_list_does_not_list_what_was_seen_behind_a_mask(self):
        # The same observations as the timeline reads, through GET /api/ai/events (#375): by camera, across cameras, and for an operator.
        with connect() as db:
            for index, box in enumerate((INSIDE, OUTSIDE)):
                db.execute("INSERT INTO ai_observations(id,camera_id,label,confidence,box_json,first_seen_at,last_seen_at,frame_width,frame_height,model_id,model_version,provider) "
                           "VALUES(?,?,?,?,?,?,?,448,256,'m','1','CPU')", (f"obs{index}", self.camera_id, "person", 0.9, json.dumps(box),
                                                                         (BASE + timedelta(seconds=index)).isoformat(), (BASE + timedelta(seconds=index + 1)).isoformat()))

        def listed(client, **params) -> list[str]:
            response = client.get("/api/ai/events", params=params)
            self.assertEqual(response.status_code, 200, response.text)
            return sorted(item["id"] for item in response.json()["events"])

        self.assertEqual(listed(self.client, camera_id=self.camera_id), ["obs0", "obs1"], "unmasked, both are listed")
        self.mask()
        self.assertEqual(listed(self.client, camera_id=self.camera_id), ["obs1"])
        self.assertEqual(listed(self.client), ["obs1"])
        self.assertEqual(listed(self.client, camera_id=self.camera_id, limit=1), ["obs1"], "the limit does not count what was hidden")
        self.assertEqual(listed(self.operator(), camera_id=self.camera_id), ["obs1"])

    def test_the_timeline_does_not_list_what_was_seen_behind_a_mask(self):
        with connect() as db:
            for index, box in enumerate((INSIDE, OUTSIDE)):
                db.execute("INSERT INTO ai_observations(id,camera_id,label,confidence,box_json,first_seen_at,last_seen_at,frame_width,frame_height,model_id,model_version,provider) "
                           "VALUES(?,?,?,?,?,?,?,448,256,'m','1','CPU')", (f"obs{index}", self.camera_id, "person", 0.9, json.dumps(box),
                                                                         (BASE + timedelta(seconds=index)).isoformat(), (BASE + timedelta(seconds=index + 1)).isoformat()))
        self.mask()
        body = {"camera_ids": [self.camera_id], "start": (BASE - timedelta(minutes=1)).isoformat(), "end": (BASE + timedelta(minutes=5)).isoformat()}
        response = self.client.post("/api/playback/timeline", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        seen = response.json()["cameras"][0]["ai_detections"]
        self.assertEqual([item["id"] for item in seen], ["obs1"])
