"""New timestamps are written in the canonical form (UTC, milliseconds, Z), path by path, and rows written in the old forms still work (#284)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.security import new_session_token, token_digest
from app.store import connect
from tests import test_native_app as native

CANONICAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class FormCase(native.NativeCase):
    def assert_canonical(self, table: str, *columns: str, where: str = "1=1") -> None:
        with connect() as db:
            rows = db.execute(f"SELECT {','.join(columns)} FROM {table} WHERE {where}").fetchall()
        self.assertTrue(rows, f"no rows in {table}")
        for row in rows:
            for column in columns:
                self.assertRegex(row[column], CANONICAL, f"{table}.{column} = {row[column]!r}")


class SessionFormTests(FormCase):
    def test_a_browser_session_expires_in_the_canonical_form(self):
        self.assert_canonical("sessions", "expires_at")

    def test_a_video_wall_session_expires_in_the_canonical_form(self):
        secret = self.admin.post("/api/admin/api-tokens", json={"name": "wall", "scopes": ["read", "media"]}).json()["secret"]
        self.assertEqual(TestClient(self.app).post("/api/wall/session", json={"token": secret}).status_code, 200)
        self.assert_canonical("wall_sessions", "expires_at")

    def test_a_phones_device_session_is_written_and_rotated_in_the_canonical_form(self):
        made = self.limited.post("/api/native/device-sessions", json={"device_label": "Pixel"}).json()
        self.assertRegex(made["expires_at"], CANONICAL)
        self.assert_canonical("device_sessions", "created_at", "expires_at")
        resumed = TestClient(self.app).post("/api/native/device-sessions/resume", json={"token": made["token"]})
        self.assertEqual(resumed.status_code, 200)
        self.assertRegex(resumed.json()["expires_at"], CANONICAL)
        self.assert_canonical("device_sessions", "last_used_at", "expires_at", "previous_until")

    def test_sessions_written_in_the_old_forms_are_still_honoured_or_refused_by_their_time(self):
        now = datetime.now(timezone.utc)
        tokens = {}
        with connect() as db:
            user = db.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
            for label, expires in (("future-old-form", (now + timedelta(hours=1)).isoformat()), ("past-old-form", (now - timedelta(hours=1)).isoformat()),
                                   ("future-z-form", (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))):
                raw = new_session_token()
                tokens[label] = raw
                db.execute("INSERT INTO sessions(session_id,token_hash,user_id,expires_at) VALUES(?,?,?,?)", (label, token_digest(raw), user, expires))
        for label, expected in (("future-old-form", 200), ("past-old-form", 401), ("future-z-form", 200)):
            client = TestClient(self.app)
            client.cookies.set("nvr_session", tokens[label])
            self.assertEqual(client.get("/api/auth/me").status_code, expected, label)


class TokenFormTests(FormCase):
    def test_a_tokens_expiry_revocation_and_use_are_in_the_canonical_form(self):
        made = self.admin.post("/api/admin/api-tokens", json={"name": "feed", "scopes": ["read"], "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
        self.assertEqual(made.status_code, 201, made.text)
        self.assertRegex(made.json()["token"]["expires_at"], CANONICAL)
        client = TestClient(self.app)
        self.assertEqual(client.get("/api/cameras", headers={"Authorization": f"Bearer {made.json()['secret']}"}).status_code, 200)
        self.assert_canonical("api_tokens", "expires_at", "last_used_at")
        self.assertEqual(self.admin.delete(f"/api/admin/api-tokens/{made.json()['token']['id']}").status_code, 200)
        self.assert_canonical("api_tokens", "revoked_at")


class AlarmFormTests(FormCase):
    def alarm(self, alarm_id: str = "a1") -> None:
        occurred = datetime(2026, 11, 12, 9, 0, 0, tzinfo=timezone.utc)
        with connect() as db:
            db.execute("INSERT INTO alarms(id,rule_name,camera_id,label,condition_type,occurred_at,box_json,clip_starts_at,clip_ends_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (alarm_id, "Gate", self.d1, "person", "zone_entry", occurred.isoformat(), "{}", (occurred - timedelta(seconds=10)).isoformat(),
                        (occurred + timedelta(seconds=20)).isoformat(), occurred.isoformat(), occurred.isoformat()))

    def test_what_is_recorded_as_people_act_on_an_alarm_is_in_the_canonical_form(self):
        self.alarm()
        self.assertEqual(self.admin.post("/api/alarms/a1/acknowledge").status_code, 200)
        self.assertEqual(self.admin.post("/api/alarms/a1/comments", json={"body": "Looking into it"}).status_code, 201)
        self.assertEqual(self.admin.post("/api/alarms/a1/resolve", json={"note": "Staff member"}).status_code, 200)
        self.assert_canonical("alarms", "acknowledged_at", "resolved_at", "updated_at")
        self.assert_canonical("alarm_history", "occurred_at")
        self.assert_canonical("alarm_comments", "created_at")

    def test_footage_stays_protected_for_the_days_after_an_alarm_is_resolved_and_then_is_not(self):
        from app import alarms
        self.alarm()
        starts, ends = "2026-11-12T09:00:00+00:00", "2026-11-12T09:05:00+00:00"
        self.assertEqual(self.admin.post("/api/alarms/a1/resolve", json={}).status_code, 200)
        with connect() as db:
            self.assertTrue(alarms.protects_footage(db, self.d1, starts, ends, alarms.resolved_kept_since(db, datetime.now(timezone.utc))))  # Resolved just now, inside the days kept
            db.execute("UPDATE alarms SET resolved_at=? WHERE id='a1'", (datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat(),))
            self.assertFalse(alarms.protects_footage(db, self.d1, starts, ends, alarms.resolved_kept_since(db, datetime.now(timezone.utc))))  # Resolved long ago, in the old form
            db.execute("UPDATE alarms SET resolved_at=NULL WHERE id='a1'")
            self.assertTrue(alarms.protects_footage(db, self.d1, starts, ends, alarms.resolved_kept_since(db, datetime.now(timezone.utc))))  # Still open
            self.assertRegex(alarms.resolved_kept_since(db, datetime.now(timezone.utc)), CANONICAL)  # Compared with resolved_at, so written in the same form
