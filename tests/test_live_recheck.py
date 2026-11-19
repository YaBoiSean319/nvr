"""An open live stream's once-a-second access check does not stop the web server's loop (#355).

Each open stream re-checks its viewer's sign-in and camera access every second. On the main live path that ran on the event loop with
two to four new database connections: every other viewer's video, every asynchronous route and the heartbeat waited for it."""
from __future__ import annotations

import asyncio
import threading
import time
from unittest import mock

from fastapi.testclient import TestClient

from app import principal as principals, privacy
from app.api import live_media
from app.security import token_digest
from app.store import connect
from tests.test_access_grants import GrantCase, LiveSocketTests


class RecheckCase(GrantCase):
    StubStream, stub_hub = LiveSocketTests.StubStream, LiveSocketTests.stub_hub  # The live hub's stand-in from the socket tests

    def session_principal(self, client: TestClient, user_id: int, role: str) -> principals.Principal:
        return principals.Principal(id=user_id, username="x", role=role, credential=("session", token_digest(client.cookies["nvr_session"])))

    def guard(self, client: TestClient | None = None, camera: str | None = None) -> privacy.StreamGuard:
        guard = privacy.StreamGuard(self.session_principal(client or self.limited, self.limited_id, "operator"), camera or self.d1, False)
        guard._access_checked = 0.0  # The second has passed: the next check reads the database
        return guard

    def due(self, guard: privacy.StreamGuard) -> privacy.StreamGuard:
        guard._access_checked = 0.0
        return guard


class OffTheLoopTests(RecheckCase):
    def test_the_check_reads_the_database_in_a_thread(self):
        seen: list[bool] = []
        real = privacy.connect

        async def run() -> bool:
            loop_thread = threading.get_ident()

            def watched():
                seen.append(threading.get_ident() == loop_thread)
                return real()

            with mock.patch.object(privacy, "connect", watched):
                return await self.guard().check()

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(seen, [False])  # One connection, and not on the loop

    def test_a_slow_database_does_not_stop_other_work_on_the_loop(self):
        real = privacy.connect

        def slow():
            time.sleep(0.4)
            return real()

        async def run() -> tuple[float, list[bool]]:
            longest, last = 0.0, time.monotonic()
            checking = [asyncio.create_task(self.guard().check()) for _ in range(6)]  # Six viewers' checks fall due together
            while not all(task.done() for task in checking):
                await asyncio.sleep(0.01)  # Another viewer's video, a route, the heartbeat
                now = time.monotonic()
                longest, last = max(longest, now - last), now
            return longest, [task.result() for task in checking]

        with mock.patch.object(privacy, "connect", slow):
            longest, answers = asyncio.run(run())
        self.assertEqual(answers, [True] * 6)
        self.assertLess(longest, 0.2, f"the loop stood still for {longest:.2f} s while the database was slow")

    def test_connecting_looks_the_viewer_and_camera_up_in_a_thread(self):
        where: list[bool] = []
        real = live_media._admit
        main = threading.get_ident()

        def admitted(*args):
            where.append(threading.current_thread().name.startswith("asyncio_") or threading.get_ident() != main)
            where.append(asyncio._get_running_loop() is None)  # No event loop runs in the thread that does the lookups
            return real(*args)

        with mock.patch.object(live_media, "_admit", admitted), self.stub_hub(), self.limited.websocket_connect(f"/api/cameras/{self.d1}/live/stream") as websocket:
            self.assertEqual(websocket.receive_json()["type"], "init")
        self.assertEqual(where, [True, True])


class OneConnectionTests(RecheckCase):
    def connections(self, guard: privacy.StreamGuard) -> int:
        opened = 0
        real_privacy, real_principal = privacy.connect, principals.connect

        def counted(real):
            def open_one():
                nonlocal opened
                opened += 1
                return real()
            return open_one

        from app import api_tokens
        with mock.patch.object(privacy, "connect", counted(real_privacy)), mock.patch.object(principals, "connect", counted(real_principal)), \
                mock.patch.object(api_tokens, "connect", counted(api_tokens.connect)):
            self.assertTrue(guard.still_valid())
        return opened

    def test_a_persons_check_uses_one_connection(self):
        self.assertEqual(self.connections(self.guard()), 1)

    def test_a_tokens_check_uses_one_connection_too(self):
        created = self.admin.post("/api/admin/api-tokens", json={"name": "wall", "scopes": ["media", "read"]})
        self.assertEqual(created.status_code, 201, created.text)
        token = principals.Principal(id=1, username="keeper", role="administrator", scopes=frozenset({"media"}), token_id=created.json()["token"]["id"],
                                     credential=("token", created.json()["token"]["id"]))
        guard = self.due(privacy.StreamGuard(token, self.d1, False))
        self.assertEqual(self.connections(guard), 1)


class StillCutOffTests(RecheckCase):
    def test_a_session_that_ended_stops_the_stream_for_that_reason(self):
        guard = self.guard()
        self.assertTrue(guard.still_valid())
        self.limited.post("/api/auth/logout")
        self.assertFalse(asyncio.run(self.due(guard).check()))
        self.assertEqual(guard.reason, "credential")

    def test_access_that_ended_stops_the_stream_for_that_reason(self):
        guard = self.guard()
        self.assertTrue(asyncio.run(guard.check()))
        self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.yard}])
        self.assertFalse(asyncio.run(self.due(guard).check()))
        self.assertEqual(guard.reason, "access")
        self.assertFalse(guard.still_valid())  # And it stays stopped

    def test_a_camera_that_is_gone_stops_an_administrators_stream(self):
        guard = self.due(privacy.StreamGuard(self.session_principal(self.admin, 1, "administrator"), self.loose, False))
        self.assertTrue(guard.still_valid())
        with connect() as db:
            db.execute("DELETE FROM cameras WHERE id=?", (self.loose,))
        self.assertFalse(self.due(guard).still_valid())
        self.assertEqual(guard.reason, "access")

    def test_within_the_second_nothing_is_read(self):
        guard = self.guard()
        self.assertTrue(guard.still_valid())
        with mock.patch.object(privacy, "connect", side_effect=AssertionError("read again within the second")):
            self.assertTrue(guard.still_valid())
            self.assertTrue(asyncio.run(guard.check()))
