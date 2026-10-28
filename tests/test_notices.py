"""Retention by purpose and generated notices (#139): purposes required when asked for, the deletion record, notices that follow the configuration."""
from __future__ import annotations

import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.migrations import m0035_retention_purpose
from app.retention import cleanup_expired_recordings
from app.store import connect
from tests.test_access_grants import GrantCase


class PurposeTests(GrantCase):
    def require(self, flag: bool = True):
        response = self.admin.put("/api/privacy/settings", json={"require_purpose": flag})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_purpose_is_required_only_when_the_option_is_on(self):
        self.assertEqual(self.admin.put(f"/api/camera-groups/{self.dock}/retention", json={"retention_days": 20}).status_code, 200)
        self.require()
        refused = self.admin.put(f"/api/camera-groups/{self.dock}/retention", json={"retention_days": 20})
        self.assertEqual(refused.status_code, 422)
        self.assertIn("purpose", refused.json()["detail"])
        saved = self.admin.put(f"/api/camera-groups/{self.dock}/retention", json={"retention_days": 20, "purpose": "Site security", "legal_basis": "Legitimate interests"})
        self.assertEqual((saved.status_code, saved.json()["purpose"], saved.json()["legal_basis"], saved.json()["needs_purpose"]), (200, "Site security", "Legitimate interests", False))
        self.assertEqual(self.admin.put(f"/api/cameras/{self.y1}/retention", json={"retention_days": 9}).status_code, 422)
        self.assertEqual(self.admin.put(f"/api/cameras/{self.y1}/retention", json={"retention_days": 9, "purpose": "Safety"}).status_code, 200)

    def test_the_site_default_cannot_lose_its_purpose_while_one_is_required(self):
        self.admin.put("/api/privacy/settings", json={"default_purpose": "Site security"})
        self.require()
        self.assertEqual(self.admin.put("/api/privacy/settings", json={"default_purpose": ""}).status_code, 422)
        self.assertEqual(self.admin.put("/api/privacy/settings", json={"default_purpose": "Safety"}).status_code, 200)

    def test_a_policy_without_a_purpose_of_its_own_uses_the_next_one_up(self):
        self.admin.put(f"/api/camera-groups/{self.dock}/retention", json={"retention_days": 20, "purpose": "Safety of drivers"})
        self.admin.put(f"/api/cameras/{self.d1}/retention", json={"retention_days": 5})
        self.assertEqual(self.admin.get(f"/api/cameras/{self.d1}/retention").json()["purpose"], "Safety of drivers")
        self.assertEqual(self.admin.get(f"/api/cameras/{self.d1}/retention").json()["retention_days"], 5)

    def test_saving_without_a_purpose_keeps_the_one_that_is_saved(self):
        self.admin.put(f"/api/camera-groups/{self.dock}/retention", json={"retention_days": 20, "purpose": "Site security"})
        self.require()
        self.assertEqual(self.admin.put(f"/api/camera-groups/{self.dock}/retention", json={"retention_days": 25}).json()["purpose"], "Site security")

    def test_existing_policies_upgrade_with_an_empty_purpose_and_a_prompt(self):
        with connect() as db:
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('group',?,14)", (self.dock,))
        self.assertFalse(self.admin.get("/api/privacy/settings").json()["require_purpose"])
        self.assertFalse(self.admin.get(f"/api/camera-groups/{self.dock}/retention").json()["needs_purpose"])
        settings = self.require()
        by_name = {item["name"]: item for item in settings["policies"]}
        self.assertEqual((by_name["Dock"]["purpose"], by_name["Dock"]["needs_purpose"]), ("", True))
        self.assertTrue(by_name["Site default"]["needs_purpose"])
        self.assertTrue(self.admin.get(f"/api/camera-groups/{self.dock}/retention").json()["needs_purpose"])
        self.assertTrue(self.admin.get(f"/api/cameras/{self.d1}/retention").json()["needs_purpose"])

    def test_the_migration_adds_empty_purposes_to_existing_policies(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE retention_policy_overrides (scope_type TEXT, scope_id TEXT, retention_days INTEGER)")
        db.execute("INSERT INTO retention_policy_overrides VALUES('group','g',14)")
        m0035_retention_purpose.upgrade(db)
        m0035_retention_purpose.verify(db)
        self.assertEqual(db.execute("SELECT purpose,legal_basis FROM retention_policy_overrides").fetchone(), ("", ""))

    def test_only_administrators_see_purposes_deletions_and_notices(self):
        for path in ("/api/privacy/settings", "/api/retention-deletions", "/api/privacy/notice", "/api/privacy/summary"):
            for client in (self.limited, self.full):
                self.assertEqual(client.get(path).status_code, 403, path)
        self.assertEqual(self.limited.put("/api/privacy/settings", json={"require_purpose": True}).status_code, 403)


class DeletionRecordTests(GrantCase):
    def footage(self, camera: str, name: str, age_days: int, size: int = 10) -> Path:
        now = datetime.now(timezone.utc)
        path = Path(os.environ["NVR_RECORDINGS_PATH"]) / camera / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"x" * size)
        with connect() as db:
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                       (camera, f"{camera}/{name}", (now - timedelta(days=age_days, minutes=1)).isoformat(), (now - timedelta(days=age_days)).isoformat(), size))
        return path

    def test_the_record_matches_what_a_retention_run_deleted(self):
        self.admin.put(f"/api/camera-groups/{self.dock}/retention", json={"retention_days": 10, "purpose": "Site security", "legal_basis": "Legitimate interests"})
        self.footage(self.d1, "old1.mp4", 20, 100)
        self.footage(self.d1, "old2.mp4", 15, 50)
        self.footage(self.d2, "old3.mp4", 12, 7)
        recent = self.footage(self.d1, "recent.mp4", 2)
        result = cleanup_expired_recordings()
        self.assertEqual(result["removed_segments"], 3)
        self.assertTrue(recent.exists())
        records = self.admin.get("/api/retention-deletions").json()
        by_camera = {item["camera_id"]: item for item in records["deletions"]}
        self.assertEqual((by_camera[self.d1]["segments"], by_camera[self.d1]["bytes"]), (2, 150))
        self.assertEqual((by_camera[self.d2]["segments"], by_camera[self.d2]["bytes"]), (1, 7))
        self.assertEqual((by_camera[self.d1]["retention_days"], by_camera[self.d1]["purpose"], by_camera[self.d1]["camera_name"]), (10, "Site security", "Dock 1"))
        self.assertEqual(records["totals"], {"segments": 3, "bytes": 157})
        self.assertEqual([item["camera_id"] for item in self.admin.get("/api/retention-deletions", params={"camera_id": self.d2}).json()["deletions"]], [self.d2])
        # Nothing left to delete: a second run adds nothing, and the record survives the camera.
        self.assertEqual(cleanup_expired_recordings()["removed_segments"], 0)
        self.assertEqual(self.admin.get("/api/retention-deletions").json()["totals"]["segments"], 3)
        self.assertEqual(self.admin.get("/api/retention-deletions", params={"start": "2999-01-01"}).json()["deletions"], [])

    def test_footage_kept_for_an_alarm_or_a_hold_is_not_recorded_as_deleted(self):
        self.footage(self.y1, "old.mp4", 90)
        with connect() as db:
            segment = db.execute("SELECT id FROM recording_segments WHERE camera_id=?", (self.y1,)).fetchone()[0]
            db.execute("INSERT INTO recording_holds(id,segment_id,camera_id) VALUES(?,?,?)", (str(uuid.uuid4()), segment, self.y1))
        self.assertEqual(cleanup_expired_recordings()["removed_segments"], 0)
        self.assertEqual(self.admin.get("/api/retention-deletions").json()["deletions"], [])


class NoticeTests(GrantCase):
    def notice(self) -> dict:
        return self.admin.get("/api/privacy/notice").json()

    def configure(self):
        response = self.admin.put("/api/privacy/settings", json={"organisation": "Harbour Depot Ltd", "contact_name": "Data Protection Officer", "contact_email": "privacy@harbour.example",
                                                                "default_purpose": "Site security"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_the_notice_follows_the_configuration_at_once(self):
        self.configure()
        text = " ".join(self.notice()["lines"])
        self.assertIn("Harbour Depot Ltd", text)
        self.assertIn("Site security", text)
        self.assertIn("up to 30 days", text)
        self.assertIn("Sound is not recorded", text)
        self.assertIn("Cameras: Dock 1, Dock 2, Loose, Yard 1.", text)
        self.assertIn("privacy@harbour.example", text)
        self.admin.put(f"/api/camera-groups/{self.dock}/retention", json={"retention_days": 90, "purpose": "Safety of drivers"})
        self.admin.put(f"/api/cameras/{self.y1}/retention", json={"retention_days": 7})
        with connect() as db:
            db.execute("UPDATE cameras SET audio_enabled=1 WHERE id=?", (self.d1,))
        data = self.notice()
        text = " ".join(data["lines"])
        self.assertEqual(data["retention_days"], 90)
        self.assertIn("up to 90 days", text)
        self.assertIn("Sound is also recorded by: Dock 1", text)
        self.assertIn("Safety of drivers", text)
        self.assertIn("Site security", text)  # The cameras still under the default purpose
        self.assertNotIn("Sound is not recorded", text)

    def test_notices_never_carry_camera_addresses_or_credentials(self):
        with connect() as db:
            db.execute("UPDATE cameras SET camera_ip=?,device_service_url=? WHERE id=?", ("10.20.30.40", "http://10.20.30.40/onvif/device_service", self.d1))
        self.configure()
        pages = [self.admin.get("/api/privacy/notice", params={"format": "html"}).text, self.admin.get("/api/privacy/summary", params={"format": "html"}).text,
                 self.admin.get("/api/privacy/notice").text, self.admin.get("/api/privacy/summary").text, self.admin.get("/api/privacy/settings").text]
        for page in pages:
            for forbidden in ("10.20.30.40", "rtsp://", "onvif", "stream_url", "password"):
                self.assertNotIn(forbidden, page.lower().replace("keeper", ""), forbidden)

    def test_the_summary_lists_cameras_purposes_audio_and_who_has_access(self):
        self.configure()
        self.admin.put(f"/api/camera-groups/{self.dock}/retention", json={"retention_days": 60, "purpose": "Safety of drivers", "legal_basis": "Legal obligation"})
        with connect() as db:
            db.execute("UPDATE cameras SET audio_enabled=1 WHERE id=?", (self.y1,))
        data = self.admin.get("/api/privacy/summary").json()
        cameras = {item["name"]: item for item in data["cameras"]}
        self.assertEqual((cameras["Dock 1"]["retention_days"], cameras["Dock 1"]["purpose"], cameras["Dock 1"]["legal_basis"]), (60, "Safety of drivers", "Legal obligation"))
        self.assertEqual((cameras["Loose"]["retention_days"], cameras["Loose"]["purpose"]), (30, "Site security"))
        self.assertEqual(data["audio_cameras"], ["Yard 1"])
        people = {item["username"]: item for item in data["people"]}
        self.assertEqual((people["keeper"]["all_cameras"], people["full"]["all_cameras"]), (True, True))
        self.assertEqual((people["limited"]["all_cameras"], people["limited"]["cameras"]), (False, 2))
        self.assertEqual(data["contact"]["email"], "privacy@harbour.example")

    def test_the_printable_pages_are_html_and_escape_what_was_entered(self):
        self.admin.put("/api/privacy/settings", json={"organisation": "<script>alert(1)</script> Depot"})
        for path in ("/api/privacy/notice", "/api/privacy/summary"):
            response = self.admin.get(path, params={"format": "html"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/html"))
            self.assertNotIn("<script>alert(1)", response.text)
            self.assertIn("&lt;script&gt;", response.text)
            self.assertRegex(response.text, re.compile(r"window\.print\(\)"))

    def test_settings_are_checked(self):
        self.assertEqual(self.admin.put("/api/privacy/settings", json={"contact_email": "not an email"}).status_code, 422)
        self.assertEqual(self.admin.put("/api/privacy/settings", json={"organisation": "x" * 400}).status_code, 422)
