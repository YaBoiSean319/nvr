"""Open live, relayed and talk streams end when the credential behind them does (#298), over real WebSocket handlers with synthetic sources."""
from __future__ import annotations

import time
from unittest import mock

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.credentials import encrypt
from app.store import connect
from tests import test_access_grants as grants, test_console as console  # Modules, not classes: importing a TestCase would run it again here
from tests.fixtures.rtsp_backchannel import BackchannelCamera
from tests.scene_case import SceneTestCase


BOUND = 5  # seconds: the documented bound is ACCESS_RECHECK_SECONDS (1 s) plus the work of one check


class LiveCredentialTests(grants.GrantCase):
    StubStream = grants.LiveSocketTests.StubStream
    stub_hub = grants.LiveSocketTests.stub_hub

    def open_stream(self, client, **kwargs):
        return client.websocket_connect(f"/api/cameras/{self.d1}/live/stream", **kwargs)

    def drain_start(self, websocket) -> None:
        self.assertEqual(websocket.receive_json()["type"], "init")
        self.assertEqual(websocket.receive_bytes(), b"init")
        self.assertEqual(websocket.receive_bytes(), b"fragment")

    def assert_cut_off(self, websocket, detail="Your sign-in has ended", code=4401) -> None:
        began = time.monotonic()
        self.assertEqual(websocket.receive_json()["detail"], detail)
        with self.assertRaises(WebSocketDisconnect) as closed:
            websocket.receive_json()
        self.assertEqual(closed.exception.code, code)
        self.assertLess(time.monotonic() - began, BOUND)

    def test_signing_out_closes_a_quiet_stream(self):
        with self.stub_hub(), self.open_stream(self.limited) as websocket:
            self.drain_start(websocket)
            self.assertEqual(self.limited.post("/api/auth/logout").status_code, 204)
            self.assert_cut_off(websocket)

    def test_an_expired_session_closes_a_quiet_stream(self):
        with self.stub_hub(), self.open_stream(self.limited) as websocket:
            self.drain_start(websocket)
            with connect() as db:
                db.execute("UPDATE sessions SET expires_at='2020-01-01T00:00:00+00:00' WHERE user_id=?", (self.limited_id,))
            self.assert_cut_off(websocket)

    def test_ending_a_persons_sessions_closes_their_stream_and_leaves_others_open(self):
        with self.stub_hub(), self.open_stream(self.limited) as cut, self.open_stream(self.full) as kept:
            self.drain_start(cut)
            self.drain_start(kept)
            with connect() as db:
                db.execute("DELETE FROM sessions WHERE user_id=?", (self.limited_id,))
            self.assert_cut_off(cut)
            kept.send_json({"type": "listen", "on": False})  # The other person's stream is still being served
            self.assertEqual(kept.receive_json()["type"], "listening")

    def test_revoking_a_token_closes_its_stream_and_a_live_token_stays_open(self):
        def make(name):
            created = self.admin.post("/api/admin/api-tokens", json={"name": name, "scopes": ["read", "media"]})
            self.assertEqual(created.status_code, 201, created.text)
            return {**created.json(), "id": created.json()["token"]["id"]}
        doomed, kept = make("doomed"), make("kept")
        client = TestClient(self.app)
        with self.stub_hub(), \
                self.open_stream(client, headers={"Authorization": f"Bearer {doomed['secret']}"}) as cut, \
                self.open_stream(client, headers={"Authorization": f"Bearer {kept['secret']}"}) as open_one:
            self.drain_start(cut)
            self.drain_start(open_one)
            self.assertEqual(self.admin.delete(f"/api/admin/api-tokens/{doomed['id']}").status_code, 200)
            self.assert_cut_off(cut)
            open_one.send_json({"type": "listen", "on": False})
            self.assertEqual(open_one.receive_json()["type"], "listening")

    def test_demoting_the_owner_of_a_token_closes_its_stream(self):
        created = self.admin.post("/api/admin/api-tokens", json={"name": "feed", "scopes": ["read", "media"]}).json()
        with self.stub_hub(), self.open_stream(TestClient(self.app), headers={"Authorization": f"Bearer {created['secret']}"}) as websocket:
            self.drain_start(websocket)
            with connect() as db:
                db.execute("UPDATE users SET role='operator' WHERE id=1")
            self.assert_cut_off(websocket)

    def test_a_sessions_stream_follows_the_roles_current_access(self):
        # Quiet stream, session still valid, grant lapses: still the existing behaviour (4403).
        with self.stub_hub(), self.open_stream(self.limited) as websocket:
            self.drain_start(websocket)
            self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.yard}])
            self.assert_cut_off(websocket, "Your access to this camera has ended", 4403)


class TalkCredentialTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        self.camera = BackchannelCamera(username="operator", password="S3cret!pass")
        self.addCleanup(self.camera.close)
        with connect() as db:
            db.execute("UPDATE cameras SET stream_url_ciphertext=? WHERE id=?", (encrypt(self.camera.url), self.camera_id))
        self.path = f"/api/cameras/{self.camera_id}/talk"
        self.stream = f"{self.path}/stream"

    def enable(self, **changes) -> None:
        body = {"enabled": True, "roles": ["administrator"], "record_audio": False} | changes
        self.assertEqual(self.client.put(self.path, json=body).status_code, 200)

    def test_signing_out_ends_a_talk_in_progress(self):
        self.enable(roles=["administrator", "operator"])
        with mock.patch("app.api.talk.SETTINGS_RECHECK_SECONDS", 0.2), self.client.websocket_connect(self.stream) as websocket:
            self.assertEqual(websocket.receive_json()["type"], "ready")
            with connect() as db:
                db.execute("DELETE FROM sessions")
            ended = websocket.receive_json()
        self.assertEqual((ended["type"], ended["reason"]), ("ended", "turned_off"))
        self.assertEqual(self.camera.torn_down, 1)  # The backchannel to the camera is closed too

    def test_a_demoted_person_is_cut_off_but_an_unchanged_one_keeps_talking(self):
        self.enable()
        with mock.patch("app.api.talk.SETTINGS_RECHECK_SECONDS", 0.2), self.client.websocket_connect(self.stream) as websocket:
            self.assertEqual(websocket.receive_json()["type"], "ready")
            time.sleep(0.8)  # Several checks pass and nothing happens
            websocket.send_bytes(b"\x00\x01" * 160)
            self.assertTrue(self.camera.wait_for(1))
            with connect() as db:
                db.execute("INSERT INTO users(username,password_hash,role) VALUES('second','x','administrator')")
                db.execute("UPDATE users SET role='operator' WHERE username='keeper'")
            ended = websocket.receive_json()
        self.assertEqual(ended["reason"], "turned_off")


class FakeUpstream:
    """Stands in for a site's WebSocket: delivers nothing (a quiet camera), and records that it was closed."""
    close_code = None
    close_reason = ""

    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        import asyncio
        await asyncio.sleep(3600)

    async def send(self, data) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class RelayCredentialTests(console.ConsoleCase):
    def relay(self, client, upstream):
        async def connect_fake(*args, **kwargs):
            return upstream
        patcher = mock.patch("app.api.console.websockets.connect", connect_fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client.websocket_connect(f"/api/cameras/{self.cid(self.a, 'Gate A1')}/live/stream")

    def test_removing_a_site_grant_ends_the_relay_and_closes_the_upstream(self):
        self.federate()
        self.grant_site(self.limited_id, self.a)
        upstream = FakeUpstream()
        with self.relay(self.limited, upstream) as websocket:
            time.sleep(1.5)  # Several checks pass with the grant in place
            self.assertFalse(upstream.closed)
            self.grant_site(self.limited_id)
            began = time.monotonic()
            self.assertEqual(websocket.receive_json()["type"], "forbidden")
            with self.assertRaises(WebSocketDisconnect):
                websocket.receive_json()
        self.assertLess(time.monotonic() - began, BOUND)
        self.assertTrue(upstream.closed)

    def test_signing_out_ends_the_relay(self):
        self.federate()
        self.grant_site(self.limited_id, self.a)
        upstream = FakeUpstream()
        with self.relay(self.limited, upstream) as websocket:
            self.assertEqual(self.limited.post("/api/auth/logout").status_code, 204)
            self.assertEqual(websocket.receive_json()["type"], "unauthorized")
            with self.assertRaises(WebSocketDisconnect) as closed:
                websocket.receive_json()
        self.assertEqual(closed.exception.code, 4401)
        self.assertTrue(upstream.closed)
