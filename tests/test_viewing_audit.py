"""The viewing audit: sessions, exports, downloads and snapshots, the two reports, the CSV and retention."""
from __future__ import annotations

import csv
import io
import shutil
import subprocess
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import viewing_audit
from app.credentials import encrypt
from app.store import connect
from tests.test_access_grants import GrantCase, START
from tests.test_support import TEST_PASSWORD

FFMPEG = shutil.which("ffmpeg")


class AuditCase(GrantCase):
    def rows(self, **where) -> list[dict]:
        clause = " AND ".join(f"{key}=?" for key in where) or "1"
        with connect() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM viewing_audit WHERE {clause} ORDER BY id", tuple(where.values()))]

    def seed(self, user_id: int | None, username: str, camera_id: str, kind: str, started: datetime, seconds: float = 0, quality: str | None = None) -> None:
        ended = started + timedelta(seconds=seconds)
        with connect() as db:
            db.execute("INSERT INTO viewing_audit(user_id,username,camera_id,camera_name,kind,started_at,ended_at,last_seen_at,quality) VALUES(?,?,?,?,?,?,?,?,?)",
                       (user_id, username, camera_id, f"name of {camera_id}", kind, viewing_audit.stamp(started), viewing_audit.stamp(ended), viewing_audit.stamp(ended), quality))

    def make_video(self, camera_id: str, minutes: int = 0) -> int:
        segment = self.add_segment(camera_id, minutes)
        path = Path(self.environment["NVR_RECORDINGS_PATH"]) / camera_id / f"{minutes}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10", "-t", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(path)],
                       check=True, timeout=30)
        return segment

    def report(self, client=None, **query):
        response = (client or self.admin).get("/api/admin/viewing-audit/report", params=query)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class LiveSessionTests(AuditCase):
    from tests.test_access_grants import LiveSocketTests as _Live
    StubStream, stub_hub = _Live.StubStream, _Live.stub_hub

    def test_a_live_tile_is_one_session_from_opening_to_closing_with_its_quality(self):
        with self.stub_hub(), self.limited.websocket_connect(f"/api/cameras/{self.d1}/live/stream?quality=high") as websocket:
            websocket.receive_json()
            websocket.receive_bytes()
            (open_row,) = self.rows()
            self.assertEqual((open_row["user_id"], open_row["camera_id"], open_row["kind"], open_row["quality"], open_row["ended_at"]),
                             (self.limited_id, self.d1, "live", "high", None))
        (closed,) = self.rows()
        self.assertIsNotNone(closed["ended_at"])
        self.assertEqual((closed["username"], closed["camera_name"]), ("limited", "Dock 1"))
        self.assertGreaterEqual(closed["ended_at"], closed["started_at"])

    def test_a_refused_tile_is_not_a_viewing(self):
        with self.stub_hub():
            self._Live.refusal(self, self.limited, self.y1)
        self.assertEqual(self.rows(), [])

    def test_a_session_a_crash_left_open_ends_when_it_was_last_seen(self):
        with connect() as db:
            session = viewing_audit.begin(db, self.limited_id, self.d1, "live", "low")
            viewing_audit.heartbeat(db, session)
            viewing_audit.close_orphans(db)
        (row,) = self.rows()
        self.assertEqual(row["ended_at"], row["last_seen_at"])


class PlaybackAndPointEventTests(AuditCase):
    @unittest.skipUnless(FFMPEG, "needs ffmpeg")
    def test_playback_requests_make_one_session_until_two_minutes_pass_without_one(self):
        first, second = self.make_video(self.d1, 0), self.make_video(self.d1, 1)
        self.assertEqual(self.limited.get(f"/api/cameras/{self.d1}/segments/{first}/media").status_code, 200)
        self.assertEqual(self.limited.get(f"/api/cameras/{self.d1}/segments/{second}/media").status_code, 200)
        self.assertEqual(len(self.rows(kind="playback")), 1)
        with connect() as db:  # Three minutes pass.
            db.execute("UPDATE viewing_audit SET ended_at=?,last_seen_at=?", (viewing_audit.stamp(datetime.now(timezone.utc) - timedelta(minutes=3)),) * 2)
        self.limited.get(f"/api/cameras/{self.d1}/segments/{first}/media")
        self.assertEqual([row["kind"] for row in self.rows()], ["playback", "playback"])

    @unittest.skipUnless(FFMPEG, "needs ffmpeg")
    def test_playback_that_could_not_be_served_is_not_recorded(self):
        missing = self.add_segment(self.d1, 5)  # Indexed, but no file on disk.
        self.assertEqual(self.limited.get(f"/api/cameras/{self.d1}/segments/{missing}/media").status_code, 410)
        self.assertEqual(self.limited.get(f"/api/cameras/{self.y1}/segments/{missing}/media").status_code, 404)
        self.assertEqual(self.rows(), [])

    @unittest.skipUnless(FFMPEG, "needs ffmpeg")
    def test_a_snapshot_is_recorded_for_its_camera(self):
        segment = self.make_video(self.d1)
        at = (START + timedelta(seconds=0.5)).isoformat()
        response = self.limited.get(f"/api/cameras/{self.d1}/segments/{segment}/snapshot", params={"at": at})
        self.assertEqual(response.status_code, 200, response.text)
        (row,) = self.rows(kind="snapshot")
        self.assertEqual((row["user_id"], row["camera_id"], row["reference"]), (self.limited_id, self.d1, response.headers["X-Audit-Reference"]))

    def export(self, client, cameras) -> str:
        self.app.state.exports = type("Queue", (), {"enqueue": staticmethod(lambda job: None)})()
        self.add_segment(self.d1)
        self.add_segment(self.d2)
        window = {"start": START.isoformat(), "end": (START + timedelta(minutes=5)).isoformat()}
        created = client.post("/api/exports", json={**window, "camera_ids": cameras})
        self.assertEqual(created.status_code, 202, created.text)
        return created.json()["id"]

    def test_an_export_is_recorded_once_per_camera_and_so_is_its_download(self):
        job = self.export(self.limited, [self.d1, self.d2])
        self.assertEqual(sorted((row["camera_id"], row["reference"]) for row in self.rows(kind="export")), sorted([(self.d1, job), (self.d2, job)]))
        with connect() as db:
            db.execute("UPDATE exports SET status='completed',expires_at=?,output_relative_path=? WHERE id=?",
                       ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), f"exports/{job}.zip", job))
        package = Path(self.environment["NVR_RECORDINGS_PATH"]) / "exports" / f"{job}.zip"
        package.parent.mkdir(parents=True, exist_ok=True)
        package.write_bytes(b"PK")
        self.assertEqual(self.limited.get(f"/api/exports/{job}/download").status_code, 200)
        self.assertEqual(sorted(row["camera_id"] for row in self.rows(kind="download")), sorted([self.d1, self.d2]))
        self.assertEqual(self.limited.get(f"/api/exports/{uuid.uuid4()}/download").status_code, 404)
        self.assertEqual(len(self.rows(kind="download")), 2)

    def test_an_export_that_fails_validation_leaves_nothing_behind(self):
        self.app.state.exports = type("Queue", (), {"enqueue": staticmethod(lambda job: None)})()
        window = {"start": START.isoformat(), "end": (START + timedelta(minutes=5)).isoformat()}
        self.assertEqual(self.limited.post("/api/exports", json={**window, "camera_ids": [self.d1]}).status_code, 422)  # No footage indexed.
        self.assertEqual(self.rows(), [])


class ReportTests(AuditCase):
    NOW = datetime.now(timezone.utc).replace(microsecond=0)

    def setUp(self):
        super().setUp()
        base = self.NOW - timedelta(hours=3)
        self.seed(self.limited_id, "limited", self.d1, "live", base, 600, "low")
        self.seed(self.limited_id, "limited", self.d2, "live", base + timedelta(minutes=20), 300, "high")
        self.seed(self.limited_id, "limited", self.d1, "playback", base + timedelta(hours=1), 120)
        self.seed(self.full_id, "full", self.d1, "live", base + timedelta(minutes=5), 60, "low")
        self.seed(self.full_id, "full", self.d1, "export", base + timedelta(minutes=30))
        self.seed(self.full_id, "full", self.d1, "download", base + timedelta(minutes=31))
        self.seed(self.full_id, "full", self.y1, "snapshot", base + timedelta(minutes=40))
        self.range = {"from": (self.NOW - timedelta(hours=4)).isoformat(), "to": self.NOW.isoformat()}

    def test_per_person_totals(self):
        report = self.report(by="person", **self.range)
        by_name = {row["username"]: row for row in report["rows"]}
        self.assertEqual((by_name["limited"]["live_sessions"], by_name["limited"]["live_seconds"], by_name["limited"]["playback_seconds"], by_name["limited"]["cameras"]), (2, 900, 120, 2))
        self.assertEqual((by_name["full"]["live_seconds"], by_name["full"]["exports"], by_name["full"]["downloads"], by_name["full"]["snapshots"], by_name["full"]["cameras"]), (60, 1, 1, 1, 2))
        self.assertEqual(report["rows"][0]["username"], "limited")  # Most viewing first.

    def test_per_camera_totals(self):
        by_camera = {row["camera_id"]: row for row in self.report(by="camera", **self.range)["rows"]}
        self.assertEqual((by_camera[self.d1]["live_seconds"], by_camera[self.d1]["playback_seconds"], by_camera[self.d1]["people"]), (660, 120, 2))
        self.assertEqual(by_camera[self.d2]["live_seconds"], 300)
        self.assertEqual(by_camera[self.y1]["snapshots"], 1)
        self.assertEqual(by_camera[self.d1]["camera_name"], f"name of {self.d1}")

    def test_the_two_reports_agree_for_the_same_range_and_filters(self):
        for query in ({}, {"user_id": self.limited_id}, {"camera_id": self.d1}, {"user_id": self.full_id, "camera_id": self.d1}):
            people, cameras = self.report(by="person", **self.range, **query), self.report(by="camera", **self.range, **query)
            self.assertEqual(people["total"], cameras["total"], query)
            for field in ("live_sessions", "live_seconds", "playback_sessions", "playback_seconds", "exports", "downloads", "snapshots"):
                self.assertAlmostEqual(sum(row[field] for row in people["rows"]), sum(row[field] for row in cameras["rows"]), msg=f"{field} {query}")
                self.assertAlmostEqual(sum(row[field] for row in people["rows"]), people["total"][field], msg=f"{field} {query}")

    def test_a_session_counts_only_the_time_inside_the_range(self):
        base = self.NOW - timedelta(hours=3)
        narrow = {"from": (base + timedelta(minutes=5)).isoformat(), "to": (base + timedelta(minutes=8)).isoformat()}  # 3 of limited's first 10 minutes, and 3 of full's 1.
        report = self.report(by="person", **narrow)
        by_name = {row["username"]: row for row in report["rows"]}
        self.assertEqual((by_name["limited"]["live_seconds"], by_name["full"]["live_seconds"]), (180, 60))
        self.assertNotIn("export", {kind for row in report["rows"] for kind in row if row[kind] and kind == "export"})

    def test_a_person_who_was_removed_stays_in_the_report_under_their_name(self):
        with connect() as db:
            db.execute("DELETE FROM users WHERE id=?", (self.full_id,))
        rows = self.report(by="person", **self.range)["rows"]
        self.assertIn(("full", None), {(row["username"], row["user_id"]) for row in rows})

    def test_entries_list_the_sessions_newest_first_with_their_length(self):
        data = self.admin.get("/api/admin/viewing-audit/entries", params={**self.range, "kind": "live"}).json()
        self.assertEqual(data["total"], 3)
        self.assertEqual([entry["seconds"] for entry in data["entries"]], [300, 60, 600])
        self.assertEqual(data["entries"][0]["quality"], "high")
        paged = self.admin.get("/api/admin/viewing-audit/entries", params={**self.range, "limit": 2, "offset": 2}).json()
        self.assertEqual((paged["total"], len(paged["entries"])), (7, 2))

    def test_the_range_must_be_sensible(self):
        for params in ({"from": "2026-01-02T00:00:00+00:00", "to": "2026-01-01T00:00:00+00:00"}, {"from": "2026-01-01T00:00:00", "to": "2026-01-02T00:00:00+00:00"}):
            self.assertEqual(self.admin.get("/api/admin/viewing-audit/report", params=params).status_code, 422)
        self.assertEqual(self.admin.get("/api/admin/viewing-audit/report").status_code, 200)  # Defaults to the last week.


class AccessAndExportTests(ReportTests):
    def test_only_administrators_see_the_report_and_the_csv_and_the_settings(self):
        for path in ("report", "entries", "export.csv", "settings"):
            self.assertEqual(self.limited.get(f"/api/admin/viewing-audit/{path}").status_code, 403, path)
        self.assertEqual(self.limited.put("/api/admin/viewing-audit/settings", json={"retention_days": 90}).status_code, 403)
        self.assertEqual(TestClientless().status(self.app, "/api/admin/viewing-audit/report"), 401)

    def test_an_api_token_needs_the_admin_scope(self):
        def token(scopes):
            created = self.admin.post("/api/admin/api-tokens", json={"name": "audit", "scopes": scopes})
            self.assertEqual(created.status_code, 201, created.text)
            return {"Authorization": f"Bearer {created.json()['secret']}"}
        from fastapi.testclient import TestClient
        client = TestClient(self.app)
        self.assertEqual(client.get("/api/admin/viewing-audit/report", headers=token(["read", "media", "export"])).status_code, 403)
        self.assertEqual(client.get("/api/admin/viewing-audit/report", headers=token(["admin"])).status_code, 200)

    def csv_rows(self, **query) -> tuple[str, list[list[str]]]:
        response = self.admin.get("/api/admin/viewing-audit/export.csv", params={**self.range, **query})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("text/csv"))
        self.assertIn("attachment", response.headers["content-disposition"])
        return response.text, list(csv.reader(io.StringIO(response.text)))

    def test_the_csv_matches_the_report_for_people_cameras_and_entries(self):
        _, people = self.csv_rows(view="person")
        report = self.report(by="person", **self.range)
        self.assertEqual(len(people) - 1, len(report["rows"]))
        self.assertEqual(people[1][:2], [report["rows"][0]["username"], str(report["rows"][0]["cameras"])])
        _, cameras = self.csv_rows(view="camera")
        self.assertEqual({row[0] for row in cameras[1:]}, {f"name of {camera}" for camera in (self.d1, self.d2, self.y1)})
        _, entries = self.csv_rows(view="entries")
        self.assertEqual(entries[0], ["person", "camera", "kind", "started_at", "ended_at", "seconds", "quality", "reference"])
        self.assertEqual(len(entries) - 1, 7)

    def test_the_csv_holds_no_camera_address_or_credential(self):
        secret = "rtsp://viewer:hunter2-secret@192.0.2.77:554/stream1"
        with connect() as db:
            db.execute("UPDATE cameras SET stream_url_ciphertext=?,live_substream_url_ciphertext=? WHERE id=?", (encrypt(secret), encrypt(secret), self.d1))
        for view in ("person", "camera", "entries"):
            text, _ = self.csv_rows(view=view)
            for forbidden in ("192.0.2.77", "hunter2", "rtsp://", "viewer:"):
                self.assertNotIn(forbidden, text, view)
            text = self.admin.get("/api/admin/viewing-audit/entries", params=self.range).text + self.report(by="camera", **self.range).__repr__()
            self.assertNotIn("hunter2", text)
            self.assertNotIn("192.0.2.77", text)

    def test_a_name_that_a_spreadsheet_would_run_as_a_formula_is_neutralised(self):
        with connect() as db:
            db.execute("UPDATE viewing_audit SET username='=HYPERLINK(\"http://x\")' WHERE user_id=?", (self.full_id,))
        _, rows = self.csv_rows(view="person")
        self.assertIn("'=HYPERLINK(\"http://x\")", {row[0] for row in rows})
        self.assertNotIn('=HYPERLINK("http://x")', {row[0] for row in rows})

    def test_downloading_the_csv_is_itself_audited(self):
        self.csv_rows(view="camera")
        (entry,) = self.audit("viewing_audit_exported")
        self.assertEqual((entry["resource_id"], entry["details"]["rows"]), ("camera", 3))


class TestClientless:
    def status(self, app, path: str) -> int:
        from fastapi.testclient import TestClient
        return TestClient(app).get(path).status_code


class RetentionTests(AuditCase):
    def setUp(self):
        super().setUp()
        now = datetime.now(timezone.utc)
        self.seed(self.full_id, "full", self.d1, "live", now - timedelta(days=400), 60)
        self.seed(self.full_id, "full", self.d1, "export", now - timedelta(days=100))
        self.seed(self.full_id, "full", self.d1, "live", now - timedelta(days=1), 60)

    def kinds_and_ages(self) -> list[str]:
        return [row["kind"] for row in self.rows()]

    def test_entries_older_than_the_retention_are_removed_and_newer_ones_kept(self):
        with connect() as db:
            self.assertEqual(viewing_audit.retention_days(db), 365)
            self.assertEqual(viewing_audit.prune(db), 1)
        self.assertEqual(self.kinds_and_ages(), ["export", "live"])

    def test_the_retention_is_configurable_and_applies_when_saved(self):
        saved = self.admin.put("/api/admin/viewing-audit/settings", json={"retention_days": 30})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["retention_days"], saved.json()["removed"]), (30, 2))
        self.assertEqual(self.kinds_and_ages(), ["live"])
        self.assertEqual(self.admin.get("/api/admin/viewing-audit/settings").json()["retention_days"], 30)
        (entry,) = self.audit("viewing_audit_retention_changed")
        self.assertEqual((entry["details"]["before"], entry["details"]["after"]), (365, 30))

    def test_the_retention_has_sane_limits(self):
        for days in (0, 6, 3651, -1):
            self.assertEqual(self.admin.put("/api/admin/viewing-audit/settings", json={"retention_days": days}).status_code, 422, days)
        self.assertEqual(len(self.rows()), 3)

    def test_an_open_session_is_not_removed_while_it_may_still_be_running(self):
        with connect() as db:
            session = viewing_audit.begin(db, self.full_id, self.d1, "live")
            self.assertEqual(viewing_audit.prune(db), 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM viewing_audit WHERE id=?", (session,)).fetchone()[0], 1)

    def test_audit_retention_does_not_touch_footage_or_the_main_audit_log(self):
        segment = self.add_segment(self.d1)
        with connect() as db:
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,created_at) VALUES(NULL,'old','x','1','2000-01-01T00:00:00.000Z')")
            viewing_audit.prune(db)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='old'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recording_segments WHERE id=?", (segment,)).fetchone()[0], 1)

    def test_the_recorders_retention_pass_prunes_it(self):
        from app.recording import RecordingManager
        RecordingManager._prune_viewing_audit()
        self.assertEqual(self.kinds_and_ages(), ["export", "live"])


class UpgradeTests(unittest.TestCase):
    def test_the_migration_creates_the_table_and_a_year_of_retention(self):
        import sqlite3
        from app.migrations import m0019_viewing_audit
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE security_settings (singleton_id INTEGER PRIMARY KEY, mfa_policy TEXT)")
        db.execute("INSERT INTO security_settings(singleton_id) VALUES (1)")
        m0019_viewing_audit.upgrade(db)
        self.assertEqual(db.execute("SELECT audit_retention_days FROM security_settings").fetchone()[0], 365)
        db.execute("INSERT INTO viewing_audit(username,camera_id,camera_name,kind,started_at,last_seen_at) VALUES('a','c','C','live','t','t')")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO viewing_audit(username,camera_id,camera_name,kind,started_at,last_seen_at) VALUES('a','c','C','peek','t','t')")


if __name__ == "__main__":
    unittest.main()
