"""The security log can be read in the product, filtered, exported and pruned (#367)."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import security_log
from app.audit import record
from app.store import connect
from tests.test_access_grants import GrantCase

PATH = "/api/admin/security-log"


class SecurityLogCase(GrantCase):
    def write(self, action: str, actor: int | None = 1, at: str | None = None, **details) -> None:
        with connect() as db:
            record(db, actor, action, "thing", "1", **details)
            if at:
                db.execute("UPDATE audit_log SET created_at=? WHERE id=(SELECT MAX(id) FROM audit_log)", (at,))

    def read(self, **query) -> dict:
        answer = self.admin.get(PATH, params=query)
        self.assertEqual(answer.status_code, 200, answer.text)
        return answer.json()


class ReadingTests(SecurityLogCase):
    def test_only_administrators_read_it(self):
        for path in (PATH, PATH + "/export.csv", PATH + "/settings"):
            self.assertEqual(self.limited.get(path).status_code, 403, path)
            self.assertEqual(TestClient(self.app).get(path).status_code, 401, path)
        self.assertEqual(self.limited.put(PATH + "/settings", json={"retention_days": 30}).status_code, 403)

    def test_rows_are_newest_first_with_who_did_them(self):
        self.write("thing_done", colour="red")
        self.write("host_thing", actor=None)
        newest = self.read()["entries"][:2]
        self.assertEqual([(row["action"], row["actor"]) for row in newest], [("host_thing", None), ("thing_done", "keeper")])
        self.assertEqual(newest[1]["details"], {"colour": "red"})

    def test_filters_by_date_person_and_action(self):
        self.write("old_thing", at="2026-01-05T10:00:00.000Z")
        self.write("thing_done", actor=self.limited_id, at="2026-02-05T10:00:00.000Z")
        self.write("thing_done", at="2026-02-06T10:00:00.000Z")
        february = {"from": "2026-02-01T00:00:00Z", "to": "2026-03-01T00:00:00Z"}
        self.assertEqual([row["action"] for row in self.read(**february)["entries"]], ["thing_done", "thing_done"])
        by_person = self.read(**february, user_id=self.limited_id)
        self.assertEqual([(row["actor"], row["at"]) for row in by_person["entries"]], [("limited", "2026-02-05T10:00:00.000Z")])
        by_action = self.read(**{"from": "2026-01-01T00:00:00Z", "to": "2026-03-01T00:00:00Z"}, action="old_thing")
        self.assertEqual((by_action["total"], by_action["entries"][0]["action"]), (1, "old_thing"))
        self.assertIn("old_thing", by_action["actions"])  # Every action the log holds is offered, not only the page's
        self.assertIn("thing_done", by_action["actions"])
        self.assertEqual(self.admin.get(PATH, params={"action": "x' OR 1=1"}).status_code, 422)

    def test_pages_and_counts(self):
        for index in range(5):
            self.write("paged_thing", at=f"2026-02-0{index + 1}T10:00:00.000Z")
        page = self.read(**{"from": "2026-02-01T00:00:00Z", "to": "2026-03-01T00:00:00Z"}, action="paged_thing", limit=2, offset=2)
        self.assertEqual((page["total"], [row["at"][:10] for row in page["entries"]]), (5, ["2026-02-03", "2026-02-02"]))

    def test_the_filters_use_the_indexes(self):
        start, end = datetime(2026, 2, 1, tzinfo=timezone.utc), datetime(2026, 3, 1, tzinfo=timezone.utc)
        with connect() as db:
            for actor, action, index in ((1, None, "audit_log_actor_idx"), (None, "thing_done", "audit_log_action_idx"), (None, None, "audit_log_created_idx")):
                where, values = security_log._where(start, end, actor, action)
                plan = " ".join(row["detail"] for row in db.execute(f"EXPLAIN QUERY PLAN SELECT l.id FROM audit_log l WHERE {where} ORDER BY l.created_at DESC LIMIT 100", values))
                self.assertIn(index, plan)
                self.assertNotIn("SCAN l", plan)

    def test_the_csv_holds_the_filtered_rows_and_cannot_run_as_a_formula(self):
        self.write("thing_done", at="2026-02-05T10:00:00.000Z", note="=HYPERLINK(\"https://example.test\")")
        with connect() as db:
            db.execute("UPDATE audit_log SET resource_id='=1+1' WHERE action='thing_done'")
        answer = self.admin.get(PATH + "/export.csv", params={"from": "2026-02-01T00:00:00Z", "to": "2026-03-01T00:00:00Z"})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertIn("attachment", answer.headers["content-disposition"])
        rows = list(csv.reader(io.StringIO(answer.text)))
        self.assertEqual(rows[0], ["at", "person", "action", "resource_type", "resource_id", "details"])
        self.assertEqual(rows[1][:5], ["2026-02-05T10:00:00.000Z", "keeper", "thing_done", "thing", "'=1+1"])
        for cell in rows[1]:
            self.assertNotIn(cell[:1], ("=", "+", "-", "@"))
        self.assertEqual(self.read(action="security_log_exported")["entries"][0]["details"]["rows"], 1)


class RetentionTests(SecurityLogCase):
    def test_the_retention_period_is_a_setting_and_changing_it_is_recorded(self):
        self.assertEqual(self.admin.get(PATH + "/settings").json(), {"retention_days": 730, "minimum": 30, "maximum": 3650})
        self.write("ancient_thing", at="2020-01-01T00:00:00.000Z")
        self.assertEqual(self.admin.put(PATH + "/settings", json={"retention_days": 29}).status_code, 422)
        saved = self.admin.put(PATH + "/settings", json={"retention_days": 90})
        self.assertEqual((saved.status_code, saved.json()["retention_days"], saved.json()["removed"]), (200, 90, 1))
        change = self.read(action="security_log_retention_changed")["entries"][0]["details"]
        self.assertEqual((change["before"], change["after"], change["removed"]), (730, 90, 1))

    def test_the_pass_removes_only_what_is_older_than_the_period(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        self.write("kept_thing", at=security_log.stamp(now - timedelta(days=729)))
        self.write("gone_thing", at=security_log.stamp(now - timedelta(days=731)))
        with connect() as db:
            before = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            self.assertEqual(security_log.prune(db, now), 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], before - 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='gone_thing'").fetchone()[0], 0)

    def test_a_pass_with_nothing_to_remove_never_writes(self):
        self.write("thing_done")
        with connect() as db:
            self.assertEqual(security_log.prune(db), 0)
            self.assertFalse(db.in_transaction)  # No write was begun, so the write lock was never taken
            plan = " ".join(row["detail"] for row in db.execute("EXPLAIN QUERY PLAN SELECT 1 FROM audit_log WHERE created_at<? LIMIT 1", ("2020",)))
            self.assertIn("audit_log_created_idx", plan)

    def test_the_recorders_retention_pass_prunes_it(self):
        from app.recording import RecordingManager
        self.write("ancient_thing", at="2020-01-01T00:00:00.000Z")
        RecordingManager._prune_viewing_audit()
        self.assertEqual(self.read(**{"from": "2019-01-01T00:00:00Z", "to": "2021-01-01T00:00:00Z"})["total"], 0)


class WhatIsWrittenTests(SecurityLogCase):
    def wrong(self, username: str) -> None:
        for _ in range(6):
            TestClient(self.app).post("/api/auth/login", json={"username": username, "password": "wrong password 12345"})

    def throttled(self) -> list[str]:
        return [row["resource_id"] for row in self.read(action="auth_throttled")["entries"]]

    def test_a_name_that_is_nobodys_is_kept_as_a_digest_and_a_real_one_by_name(self):
        typed = "MyActualPassword-2026"  # A password typed into the username box
        self.wrong(typed)
        self.wrong("keeper")
        with connect() as db:
            everything = " ".join(str(value) for row in db.execute("SELECT * FROM audit_log") for value in tuple(row))
        self.assertNotIn(typed.casefold(), everything.casefold())
        kept = self.throttled()
        self.assertTrue(any(key.startswith("unknown:") for key in kept), kept)
        self.assertTrue(any(key.startswith("keeper|") for key in kept), kept)

    def test_the_digest_is_keyed_and_the_same_for_the_same_text(self):
        import hashlib
        first = security_log.unknown_name("someone")
        self.assertEqual(first, security_log.unknown_name("someone"))
        self.assertNotEqual(first, security_log.unknown_name("someone else"))
        self.assertNotIn(hashlib.sha256(b"someone").hexdigest()[:16], first)  # Not a plain hash anyone could look up

    def test_ending_another_persons_session_is_recorded(self):
        listed = self.admin.get("/api/admin/sessions").json()["sessions"]
        theirs = next(row for row in listed if row["username"] == "limited")
        self.assertEqual(self.admin.delete(f"/api/admin/sessions/{theirs['session_id']}").status_code, 204)
        self.assertEqual(self.limited.get("/api/auth/me").status_code, 401)
        entry = self.read(action="session_ended")["entries"][0]
        self.assertEqual((entry["actor"], entry["details"]["username"], entry["resource_id"]), ("keeper", "limited", str(self.limited_id)))
        self.assertEqual(self.admin.delete("/api/admin/sessions/no-such-session").status_code, 204)
        self.assertEqual(self.read(action="session_ended")["total"], 1)

    def test_changing_display_settings_is_recorded_once_per_real_change(self):
        current = self.admin.get("/api/settings/display").json()
        body = {"time_zone": "Europe/London", "date_format": current["date_format"], "clock_format": "24"}
        self.assertEqual(self.admin.put("/api/settings/display", json=body).status_code, 200)
        self.assertEqual(self.admin.put("/api/settings/display", json=body).status_code, 200)  # Saved again unchanged
        entries = self.read(action="display_settings_changed")
        self.assertEqual(entries["total"], 1)
        self.assertEqual((entries["entries"][0]["details"]["after"]["time_zone"], entries["entries"][0]["details"]["after"]["clock_format"]), ("Europe/London", "24"))
