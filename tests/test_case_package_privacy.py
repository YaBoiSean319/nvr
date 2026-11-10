"""A case package is served with today's camera access and privacy masks, not those of the day it was built (#302)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app import cases
from app.store import connect
from tests.test_cases import PackageCase

FULL_FRAME = {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}


class StalePackageCase(PackageCase):
    """The base class's setUp makes a case with two clips on Dock 1; these tests build on it."""

    def download(self, client=None, case=None, package=None):
        return (client or self.full).get(f"/api/cases/{case or self.case}/packages/{package}/download")

    def downloads_recorded(self) -> int:
        return len(self.case_audit(self.case, "package_downloaded"))

    def mask(self, camera: str, *zones: dict) -> None:
        saved = self.admin.put(f"/api/cameras/{camera}/privacy", json={"zones": list(zones)})
        self.assertEqual(saved.status_code, 200, saved.text)

    def revoke(self, user_id: int, *grants: dict) -> None:
        self.grant(user_id, list(grants))


class AccessAtDownloadTests(StalePackageCase):
    def test_the_reproduction_removing_every_grant_stops_the_download(self):
        package, first = self.build(self.case)
        recorded = self.downloads_recorded()
        self.revoke(self.full_id)  # The administrator removes every camera grant from that operator
        denied = self.download(package=package)
        self.assertEqual(denied.status_code, 404, "the package holds footage the person can no longer see")
        self.assertEqual(self.downloads_recorded(), recorded, "a refused request is not a download")

    def test_an_administrator_can_still_download_what_an_operator_built(self):
        package, content = self.build(self.case)
        self.revoke(self.full_id)
        self.assertEqual(self.download(self.admin, package=package).content, content)

    def test_an_expired_grant_stops_it_too(self):
        package, _ = self.build(self.case)
        self.revoke(self.full_id, {"scope_type": "group", "group_id": self.dock, "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        self.assertEqual(self.download(package=package).status_code, 200, "still inside its grant")
        with connect() as db:
            db.execute("UPDATE access_grants SET expires_at=? WHERE expires_at IS NOT NULL", ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
        self.assertEqual(self.download(package=package).status_code, 404)

    def test_one_camera_no_longer_allowed_denies_the_whole_download(self):
        self.clip(self.case, self.y1, 0, 1, label="Yard")  # A mixed-camera package: Dock 1 (group Dock) and Yard 1 (group Yard)
        package, _ = self.build(self.case)
        self.assertEqual(self.download(package=package).status_code, 200)
        self.revoke(self.full_id, {"scope_type": "group", "group_id": self.dock})  # Dock is kept, Yard is gone
        self.assertEqual(self.download(package=package).status_code, 404, "all of it or none of it")
        self.revoke(self.full_id, {"scope_type": "group", "group_id": self.dock}, {"scope_type": "group", "group_id": self.yard})
        self.assertEqual(self.download(package=package).status_code, 200, "both allowed again")

    def test_a_camera_leaving_the_group_a_grant_names_ends_access_to_its_package(self):
        limited_case = self.create(self.limited, "Dock only")["id"]
        self.clip(limited_case, self.d1, 0, 1, client=self.limited, label="Door")
        package, _ = self.build(limited_case, self.limited)
        self.assertEqual(self.download(self.limited, limited_case, package).status_code, 200)
        with connect() as db:  # Dock 1 moves to the Yard group: out of the one the operator's grant names
            db.execute("UPDATE cameras SET group_id=? WHERE id=?", (self.yard, self.d1))
        self.assertEqual(self.download(self.limited, limited_case, package).status_code, 404)

    def test_a_package_stays_governed_after_its_clips_are_removed_from_the_case(self):
        package, _ = self.build(self.case)
        for item in self.full.get(f"/api/cases/{self.case}").json()["items"]:
            if item["kind"] == "clip":
                self.assertEqual(self.full.delete(f"/api/cases/{self.case}/items/{item['id']}").status_code, 200)
        self.revoke(self.full_id)
        self.assertEqual(self.download(package=package).status_code, 404, "the camera is recorded with the package, not read from the items")

    def test_the_requester_and_membership_rules_are_unchanged(self):
        package, _ = self.build(self.case)
        self.assertEqual(self.download(self.limited, package=package).status_code, 404, "someone else's package")
        self.assertEqual(self.download(package=package).status_code, 200)


class MaskAtDownloadTests(StalePackageCase):
    def test_a_mask_added_after_the_package_was_built_blocks_the_download(self):
        package, _ = self.build(self.case)
        recorded = self.downloads_recorded()
        self.mask(self.d1, FULL_FRAME)  # The ticket's second check: a nonempty full-frame mask newer than the package
        blocked = self.download(package=package)
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertIn("build the package again", blocked.json()["detail"].lower())
        self.assertEqual(self.downloads_recorded(), recorded)

    def test_a_mask_on_a_different_camera_does_not_matter(self):
        package, _ = self.build(self.case)
        self.mask(self.y1, FULL_FRAME)
        self.assertEqual(self.download(package=package).status_code, 200)

    def test_a_mask_that_was_removed_no_longer_blocks_it(self):
        package, _ = self.build(self.case)
        self.mask(self.d1, FULL_FRAME)
        self.assertEqual(self.download(package=package).status_code, 409)
        self.mask(self.d1)
        self.assertEqual(self.download(package=package).status_code, 200, "no mask now: nothing the footage carries is hidden")

    def test_a_mask_in_place_before_the_package_was_built_is_fine(self):
        self.mask(self.d1, {"x": 0.0, "y": 0.0, "width": 0.3, "height": 0.3})
        package, _ = self.build(self.case)
        self.assertEqual(self.download(package=package).status_code, 200, "the exports were made with it")

    def test_an_unchanged_package_downloads_identically_and_is_recorded(self):
        package, content = self.build(self.case)
        recorded = self.downloads_recorded()
        again = self.download(package=package)
        self.assertEqual((again.status_code, again.content), (200, content))
        self.assertEqual(self.downloads_recorded(), recorded + 1)

    def test_a_mixed_package_is_blocked_by_a_mask_on_either_camera(self):
        self.clip(self.case, self.y1, 0, 1, label="Yard")
        package, _ = self.build(self.case)
        self.mask(self.y1, FULL_FRAME)
        self.assertEqual(self.download(package=package).status_code, 409)


class AssemblyTests(StalePackageCase):
    def test_a_mask_that_changes_between_the_exports_and_the_assembly_fails_the_package(self):
        package = self.package(self.case)
        cases.advance_packages()      # The exports are started...
        self.finish_exports()         # ...and finish, unmasked
        self.mask(self.d1, FULL_FRAME)  # A mask is drawn before the package is put together
        cases.advance_packages()
        status = self.full.get(f"/api/cases/{self.case}/packages/{package}").json()
        self.assertEqual(status["status"], "failed", status)
        self.assertIn("privacy mask", status["error"].lower())

    def test_access_ending_before_assembly_fails_the_package(self):
        package = self.package(self.case)
        cases.advance_packages()
        self.finish_exports()
        self.revoke(self.full_id)
        cases.advance_packages()
        status = self.admin.get(f"/api/cases/{self.case}/packages/{package}")
        self.assertIn(status.status_code, (200, 404))
        with connect() as db:
            row = db.execute("SELECT status,error FROM case_packages WHERE id=?", (package,)).fetchone()
        self.assertEqual(row["status"], "failed", dict(row))

    def test_the_package_records_which_cameras_and_masks_it_was_built_with(self):
        self.mask(self.d1, {"x": 0.0, "y": 0.0, "width": 0.3, "height": 0.3})
        package, _ = self.build(self.case)
        with connect() as db:
            provenance = json.loads(db.execute("SELECT provenance_json FROM case_packages WHERE id=?", (package,)).fetchone()["provenance_json"])
        self.assertEqual(provenance["cameras"], [self.d1])
        self.assertIn(self.d1, provenance["rendered_at"])
        self.assertIsNotNone(provenance["masks"][self.d1], "the version of the mask it carries")
