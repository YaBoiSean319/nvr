"""Alarm-driven Live layouts and the video wall (#112)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.alarms import create_alarm
from app.credentials import encrypt
from app.store import connect
from tests.scene_case import SceneTestCase
from tests.test_support import TEST_PASSWORD

BOTTOM_LEFT = {"x": 0.0, "y": 0.6, "width": 0.3, "height": 0.4}


class LiveAlarmTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        self.porch = self.add_camera("Porch")
        self.drive = self.add_camera("Drive")
        self.rule_id = self.add_rule("Gate entry", pop_into_live=True)

    def add_camera(self, name: str) -> str:
        camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (camera_id, name, encrypt("rtsp://camera.invalid/main")))
        return camera_id

    def add_rule(self, name: str, *, pop_into_live: bool) -> str:
        response = self.client.post("/api/rules", json={
            "name": name, "enabled": True, "camera_ids": [self.camera_id], "group_ids": [], "classes": ["vehicle"],
            "condition": {"type": "zone_entry", "zone": BOTTOM_LEFT}, "schedule": None, "cooldown_seconds": 0, "pop_into_live": pop_into_live,
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["pop_into_live"], pop_into_live)
        return response.json()["id"]

    def raise_alarm(self, rule_id: str | None = None) -> str:
        rule_id = rule_id or self.rule_id
        event_id = str(uuid.uuid4())
        with connect() as db:
            name = db.execute("SELECT name FROM rules WHERE id=?", (rule_id,)).fetchone()["name"]
            db.execute(
                "INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, rule_id, name, self.camera_id, f"track-{event_id}", "vehicle", "zone_entry",
                 datetime.now(timezone.utc).isoformat(), '{"x":0.1,"y":0.7,"width":0.2,"height":0.15}'),
            )
            return create_alarm(db, event_id)

    def operator(self) -> TestClient:
        from app.security import hash_password

        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", ("watcher", hash_password(TEST_PASSWORD), "operator"))
        context = TestClient(self.client.app)
        client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        signed_in = client.post("/api/auth/login", json={"username": "watcher", "password": TEST_PASSWORD})
        self.assertEqual(signed_in.status_code, 200, signed_in.text)
        return client

    def pop_ins(self, client: TestClient, after: str | None = None) -> dict:
        response = client.get("/api/live/alarm-pop-ins", params={"after": after} if after else {})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_administrators_choose_each_cameras_neighbours_in_order(self):
        saved = self.client.put(f"/api/cameras/{self.camera_id}/neighbours", json={"camera_ids": [self.drive, self.porch]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["camera_ids"], [self.drive, self.porch])
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera_id}/neighbours").json()["camera_ids"], [self.drive, self.porch])

        for invalid in ([self.camera_id], ["no-such-camera"], [self.porch, self.porch], [str(uuid.uuid4()) for _ in range(16)]):
            refused = self.client.put(f"/api/cameras/{self.camera_id}/neighbours", json={"camera_ids": invalid})
            self.assertEqual(refused.status_code, 422, invalid)

        operator = self.operator()
        self.assertEqual(operator.get(f"/api/cameras/{self.camera_id}/neighbours").json()["camera_ids"], [self.drive, self.porch])
        self.assertEqual(operator.put(f"/api/cameras/{self.camera_id}/neighbours", json={"camera_ids": []}).status_code, 403)

        # A deleted camera leaves every neighbour list.
        with connect() as db:
            db.execute("DELETE FROM cameras WHERE id=?", (self.drive,))
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera_id}/neighbours").json()["camera_ids"], [self.porch])

    def test_pop_ins_bring_new_alarms_from_pop_in_rules_with_the_cameras_neighbours(self):
        self.client.put(f"/api/cameras/{self.camera_id}/neighbours", json={"camera_ids": [self.drive, self.porch]})
        quiet_rule = self.add_rule("Quiet", pop_into_live=False)
        self.raise_alarm()  # Raised before the operator started watching: never replayed.
        operator = self.operator()

        start = self.pop_ins(operator)
        self.assertEqual(start["alarms"], [])

        first = self.raise_alarm()
        # A clock that stepped back stamps an earlier time; the alarm is still delivered in turn.
        with connect() as db:
            db.execute("UPDATE alarms SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (first,))
        self.raise_alarm(quiet_rule)
        with connect() as db:
            db.execute("UPDATE cameras SET archived_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), self.porch))
        second = self.raise_alarm()

        batch = self.pop_ins(operator, start["cursor"])
        self.assertEqual([alarm["id"] for alarm in batch["alarms"]], [first, second])
        alarm = batch["alarms"][0]
        self.assertEqual((alarm["rule_name"], alarm["camera_id"], alarm["label"]), ("Gate entry", self.camera_id, "vehicle"))
        # Archived neighbours are left out.
        self.assertEqual(alarm["camera_ids"], [self.camera_id, self.drive])
        self.assertEqual(self.pop_ins(operator, batch["cursor"])["alarms"], [])

        # Turning pop-in off for the rule stops later alarms only.
        rule = self.client.get(f"/api/rules/{self.rule_id}").json()
        payload = {key: rule[key] for key in ("name", "enabled", "camera_ids", "group_ids", "condition", "classes", "schedule", "cooldown_seconds")}
        self.assertEqual(self.client.put(f"/api/rules/{self.rule_id}", json={**payload, "pop_into_live": False}).status_code, 200)
        self.raise_alarm()
        self.assertEqual(self.pop_ins(operator, batch["cursor"])["alarms"], [])
        self.assertEqual(len(self.pop_ins(operator, start["cursor"])["alarms"]), 2)

        self.assertEqual(operator.get("/api/live/alarm-pop-ins", params={"after": "not-a-cursor"}).status_code, 422)

    def test_each_person_opts_in_to_alarm_pop_ins(self):
        operator = self.operator()
        self.assertEqual(operator.get("/api/me/preferences").json(), {"theme": "system", "live_alarm_pop_in": False})
        saved = operator.put("/api/me/preferences", json={"live_alarm_pop_in": True})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json(), {"theme": "system", "live_alarm_pop_in": True})
        self.assertEqual(operator.put("/api/me/preferences", json={"theme": "dark"}).json(), {"theme": "dark", "live_alarm_pop_in": True})
        self.assertEqual(self.client.get("/api/me/preferences").json()["live_alarm_pop_in"], False)


class VideoWallTests(SceneTestCase):
    def token(self, *scopes: str) -> tuple[str, str]:
        response = self.client.post("/api/admin/api-tokens", json={"name": "Lobby wall", "scopes": list(scopes)})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["token"]["id"], response.json()["secret"]

    def wall(self) -> TestClient:
        context = TestClient(self.client.app)
        client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return client

    def test_a_read_and_media_token_signs_a_wall_in_to_watch_but_nothing_else(self):
        token_id, secret = self.token("read", "media")
        wall = self.wall()
        self.assertEqual(wall.get("/api/wall/session").status_code, 401)
        signed_in = wall.post("/api/wall/session", json={"token": secret})
        self.assertEqual(signed_in.status_code, 200, signed_in.text)
        self.assertEqual(signed_in.json(), {"name": "Lobby wall", "scopes": ["read", "media"]})
        self.assertNotIn(secret, json.dumps(dict(wall.cookies)))
        self.assertEqual(wall.get("/api/wall/session").json()["name"], "Lobby wall")

        # Watching works: cameras, shared views, live status and alarm pop-ins.
        self.assertEqual(wall.get("/api/cameras").status_code, 200)
        self.assertEqual(wall.get("/api/live/views").status_code, 200)
        self.assertEqual(wall.get(f"/api/cameras/{self.camera_id}/live/status").status_code, 200)
        self.assertEqual(wall.get("/api/live/alarm-pop-ins").status_code, 200)
        # Nothing else: administration, changes, or the main interface.
        self.assertEqual(wall.get("/api/rules").status_code, 403)
        # The token's administrator's own data stays theirs.
        self.assertEqual(wall.get("/api/me/preferences").status_code, 403)
        self.assertEqual(wall.get("/api/live/workspace").status_code, 403)
        self.assertEqual(wall.get("/api/alarms").status_code, 403)
        self.assertEqual(wall.put("/api/me/preferences", json={"theme": "dark"}).status_code, 403)
        self.assertEqual(wall.get("/api/auth/me").status_code, 401)

        # Revoking the token ends the wall's session.
        self.assertEqual(self.client.delete(f"/api/admin/api-tokens/{token_id}").status_code, 200)
        self.assertEqual(wall.get("/api/cameras").status_code, 401)
        self.assertEqual(wall.get("/api/wall/session").status_code, 401)

    def test_a_wall_refuses_tokens_with_any_other_scope(self):
        wall = self.wall()
        for scopes in (("read", "media", "export"), ("admin",), ("read", "media", "admin"), ("read",), ("media",)):
            _, secret = self.token(*scopes)
            refused = wall.post("/api/wall/session", json={"token": secret})
            self.assertEqual(refused.status_code, 403, scopes)
            self.assertIn("read", refused.json()["detail"])
            self.assertNotIn("nvr_wall", wall.cookies)
        self.assertEqual(wall.post("/api/wall/session", json={"token": "nvrt_nope_nope"}).status_code, 401)

    def test_signing_a_wall_out_ends_its_session(self):
        _, secret = self.token("read", "media")
        wall = self.wall()
        wall.post("/api/wall/session", json={"token": secret})
        self.assertEqual(wall.delete("/api/wall/session").status_code, 204)
        self.assertEqual(wall.get("/api/cameras").status_code, 401)

    def test_a_person_signed_in_on_the_same_browser_keeps_their_own_access(self):
        _, secret = self.token("read", "media")
        self.assertEqual(self.client.post("/api/wall/session", json={"token": secret}).status_code, 200)
        self.assertEqual(self.client.get("/api/rules").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").json()["username"], "keeper")
