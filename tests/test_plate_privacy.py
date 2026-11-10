"""A mask added after a plate was read still protects it: the picture and the plate text are checked when they are viewed (#297)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.store import connect
from tests.test_plates import PlateCase

OVER_THE_PLATE = {"x": 0.35, "y": 0.55, "width": 0.3, "height": 0.2}
TOUCHING_ITS_EDGE = {"x": 0.45, "y": 0.6, "width": 0.2, "height": 0.2}   # overlaps the plate's right half
ELSEWHERE = {"x": 0.0, "y": 0.0, "width": 0.2, "height": 0.2}


class PlatePrivacyCase(PlateCase):
    def setUp(self):
        super().setUp()
        self.read(self.d1, "ABC123")
        with connect() as db:
            self.read_id = db.execute("SELECT id FROM plate_reads WHERE plate='ABC123'").fetchone()["id"]

    def mask(self, camera: str, *zones: dict) -> None:
        saved = self.admin.put(f"/api/cameras/{camera}/privacy", json={"zones": list(zones)})
        self.assertEqual(saved.status_code, 200, saved.text)

    def crop(self, client) -> int:
        return client.get(f"/api/plates/reads/{self.read_id}/crop").status_code

    def found(self, client) -> list[str]:
        return [item["plate"] for item in self.search("ABC", client=client)]

    def tokened(self, path: str, *scopes: str):
        secret = self.admin.post("/api/admin/api-tokens", json={"name": "-".join(scopes), "scopes": list(scopes)}).json()["secret"]
        with TestClient(self.app) as bare:
            return bare.get(path, headers={"Authorization": f"Bearer {secret}"})


class HistoricalReadTests(PlatePrivacyCase):
    def test_a_read_made_before_the_mask_is_hidden_once_the_mask_covers_it(self):
        for client in (self.admin, self.limited):
            self.assertEqual((self.crop(client), self.found(client)), (200, ["ABC123"]), "before the mask, the read and its picture are there")
        self.mask(self.d1, OVER_THE_PLATE)  # The reproduction: a full mask after the capture returned the exact original crop
        for client in (self.admin, self.limited):
            self.assertEqual(self.crop(client), 404)
            self.assertEqual(self.found(client), [], "the plate text is as private as its picture")

    def test_touching_the_edge_of_a_mask_is_enough(self):
        self.mask(self.d1, TOUCHING_ITS_EDGE)
        self.assertEqual((self.crop(self.admin), self.found(self.admin)), (404, []))

    def test_a_read_outside_every_mask_is_unaffected(self):
        self.mask(self.d1, ELSEWHERE)
        self.assertEqual((self.crop(self.admin), self.found(self.admin)), (200, ["ABC123"]))

    def test_a_mask_on_another_camera_changes_nothing_here(self):
        self.read(self.d2, "XYZ789")
        self.mask(self.d2, OVER_THE_PLATE)
        self.assertEqual(self.found(self.admin), ["ABC123"], "the read on the camera that is masked is gone; this camera's stays")

    def test_removing_the_mask_brings_the_read_back(self):
        self.mask(self.d1, OVER_THE_PLATE)
        self.assertEqual(self.found(self.admin), [])
        self.mask(self.d1)
        self.assertEqual((self.crop(self.admin), self.found(self.admin)), (200, ["ABC123"]))

    def test_search_limits_do_not_count_what_was_hidden(self):
        for text in ("ABC100", "ABC200", "ABC300"):
            self.read(self.d2, text)
        self.mask(self.d1, OVER_THE_PLATE)
        response = self.admin.get("/api/plates/search", params={"q": "ABC", "limit": 3}).json()
        self.assertEqual((len(response["results"]), response["truncated"]), (3, False), "the hidden read was not counted into a full page")

    def test_a_camera_the_person_cannot_see_is_still_not_found(self):
        self.read(self.y1, "HID001")
        with connect() as db:
            hidden = db.execute("SELECT id FROM plate_reads WHERE plate='HID001'").fetchone()["id"]
        self.assertEqual(self.limited.get(f"/api/plates/reads/{hidden}/crop").status_code, 404)
        self.assertEqual(self.admin.get(f"/api/plates/reads/{hidden}/crop").status_code, 200)


class UnsafeMetadataTests(PlatePrivacyCase):
    def set_box(self, text: str) -> None:
        with connect() as db:
            db.execute("UPDATE plate_reads SET box_json=? WHERE id=?", (text, self.read_id))

    def test_a_box_that_cannot_be_read_hides_the_read_on_a_masked_camera(self):
        self.mask(self.d1, ELSEWHERE)  # A mask exists, and the read's place cannot be compared with it: fail closed
        for broken in ("not json", "{}", '{"x": 0.4}', '{"x": "a", "y": 0, "width": 1, "height": 1}', "null"):
            self.set_box(broken)
            self.assertEqual((self.crop(self.admin), self.found(self.admin)), (404, []), f"box_json={broken!r}")

    def test_the_same_box_is_harmless_on_a_camera_with_no_mask(self):
        self.set_box("not json")
        self.assertEqual(self.crop(self.admin), 200)
        self.assertEqual(self.found(self.admin), ["ABC123"], "no mask, nothing to protect, and no server error from the legacy box")


class ScopeAndMaskAreIndependentTests(PlatePrivacyCase):
    def test_the_media_scope_does_not_get_past_a_mask(self):
        path = f"/api/plates/reads/{self.read_id}/crop"
        self.assertEqual(self.tokened(path, "media").status_code, 200)
        self.mask(self.d1, OVER_THE_PLATE)
        self.assertEqual(self.tokened(path, "media").status_code, 404)
        self.assertEqual(self.tokened(path, "admin").status_code, 404)

    def test_a_mask_does_not_stand_in_for_the_scope(self):
        self.mask(self.d1, ELSEWHERE)
        self.assertEqual(self.tokened(f"/api/plates/reads/{self.read_id}/crop", "read").status_code, 403)
