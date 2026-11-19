"""A request that has to wait for the database's write lock does not stop the web process (#354).

Handlers declared asynchronous that called the database directly ran on the event loop: while one waited for the lock, every page, every live
stream and the worker's own heartbeat waited with it. Each test holds the lock from another connection and watches the loop keep turning."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid

import httpx

from app import external_events
from app.store import database_path
from tests.test_access_grants import GrantCase

HOLD_SECONDS = 1.0
SECRET = "inbound-secret-for-the-loop-test"


class LoopStaysFreeTests(GrantCase):
    def hold_write_lock(self) -> threading.Thread:
        """Another process writing: the lock is taken now and kept for a second."""
        taken = threading.Event()

        def hold() -> None:
            other = sqlite3.connect(database_path(), timeout=5, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                taken.set()
                time.sleep(HOLD_SECONDS)
                other.execute("ROLLBACK")
            finally:
                other.close()

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(taken.wait(5))
        return thread

    def longest_stall_during(self, request) -> tuple[httpx.Response, float]:
        """Make the request while the write lock is held; return its answer and the longest the event loop went without turning."""

        async def body():
            longest = 0.0
            running = True

            async def ticker():
                nonlocal longest
                last = time.monotonic()
                while running:
                    await asyncio.sleep(0.01)
                    now = time.monotonic()
                    longest, last = max(longest, now - last), now

            watch = asyncio.create_task(ticker())
            holder = self.hold_write_lock()
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver") as client:
                    response = await request(client)
            finally:
                running = False
                await watch
                await asyncio.to_thread(holder.join)
            return response, longest

        return asyncio.run(body())

    def assert_free(self, name: str, request, expected_status: int) -> None:
        began = time.monotonic()
        response, stall = self.longest_stall_during(request)
        self.assertEqual(response.status_code, expected_status, f"{name}: {response.text}")
        self.assertGreater(time.monotonic() - began, HOLD_SECONDS * 0.8, f"{name}: the request did not have to wait for the lock, so this proves nothing")
        self.assertLess(stall, HOLD_SECONDS / 2, f"{name}: the event loop stopped for {stall:.2f} s while the request waited for the database")

    def test_a_signed_inbound_event_waits_for_the_lock_without_stopping_the_loop(self):
        created = self.admin.post("/api/event-sources", json={"name": "Door controller", "kind": "http", "secret": SECRET, "mappings": [{"subject": "main-door", "camera_ids": [self.d1]}]})
        self.assertEqual(created.status_code, 201, created.text)
        payload = json.dumps({"event_id": str(uuid.uuid4()), "type": "door_forced", "subject": "main-door", "message": "Forced open"}).encode()
        headers = {"Content-Type": "application/json", **external_events.sign(SECRET, payload, None)}
        self.assert_free("inbound event", lambda client: client.post(f"/api/inbound/events/{created.json()['id']}", content=payload, headers=headers), 202)

    def test_starting_a_bulk_add_waits_for_the_lock_without_stopping_the_loop(self):
        cookies = dict(self.admin.cookies)
        body = {"cameras": [{"host": f"192.0.2.{number}", "username": "viewer", "password": "camera-pass-1"} for number in range(10, 20)]}
        self.assert_free("bulk add", lambda client: client.post("/api/cameras/bulk-jobs", json=body, cookies=cookies), 201)
