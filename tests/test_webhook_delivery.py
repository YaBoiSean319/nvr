"""Incident alerts are not lost when a name cannot be looked up, and one receiver that does not answer does not hold up the rest (#379)."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

from app import webhooks
from app.credentials import encrypt
from app.store import connect, initialize

_ENV = ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH")
LITERAL = "http://198.51.100.10/hook"  # A receiver named by its address (a documentation range), which the administrator has allowed


class DeliveryCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.temporary.name) / "credentials.key")
        initialize()
        self.moment = 0

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def receiver(self, name: str, url: str = LITERAL) -> str:
        endpoint = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO webhook_endpoints(id,name,target_url_ciphertext,signing_secret_ciphertext,events_json,allow_private,enabled,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,1,1,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')", (endpoint, name, encrypt(url), encrypt("secret"), '["webhook.test"]'))
        return endpoint

    def queue(self, endpoint: str) -> str:
        self.moment += 1
        delivery = str(uuid.uuid4())
        at = f"2026-01-01T00:00:{self.moment:02d}+00:00"
        with connect() as db:
            db.execute("INSERT INTO webhook_deliveries(id,endpoint_id,endpoint_name,event_id,event_type,payload_json,status,attempt_count,next_attempt_at,created_at,updated_at) "
                       "VALUES(?,?,'r',?,'webhook.test','{}','queued',0,?,?,?)", (delivery, endpoint, str(uuid.uuid4()), at, at, at))
        return delivery

    def state(self, delivery: str) -> tuple[str, str | None, int]:
        with connect() as db:
            row = db.execute("SELECT status,error_code,attempt_count FROM webhook_deliveries WHERE id=?", (delivery,)).fetchone()
        return row["status"], row["error_code"], row["attempt_count"]

    def answering(self, handler):
        real = httpx.AsyncClient
        return patch("app.webhooks.httpx.AsyncClient", side_effect=lambda *args, **kwargs: real(*args, **{**kwargs, "transport": httpx.MockTransport(handler)}))


class UnresolvedNameTests(DeliveryCase):
    async def test_a_name_that_cannot_be_looked_up_at_delivery_time_is_tried_again(self):
        delivery = self.queue(self.receiver("cloud", "https://alerts.example.test/hook"))

        def no_dns(*args, **kwargs):
            raise OSError("Temporary failure in name resolution")

        with patch("app.webhooks.socket.getaddrinfo", no_dns):
            await webhooks.WebhookManager()._deliver(webhooks.claim_due_delivery())
        self.assertEqual(self.state(delivery), ("queued", "destination_unresolved", 1))

    async def test_a_delivery_that_was_put_off_goes_out_on_a_later_try(self):
        delivery = self.queue(self.receiver("cloud", "https://alerts.example.test/hook"))

        def no_dns(*args, **kwargs):
            raise OSError("Temporary failure in name resolution")

        with patch("app.webhooks.socket.getaddrinfo", no_dns):
            await webhooks.WebhookManager()._deliver(webhooks.claim_due_delivery())
        with connect() as db:  # The uplink is back: the receiver is reached by its address, and the wait before the next try is over
            db.execute("UPDATE webhook_endpoints SET target_url_ciphertext=?", (encrypt(LITERAL),))
            db.execute("UPDATE webhook_deliveries SET next_attempt_at='2026-01-01T00:00:00+00:00'")
        with self.answering(lambda request: httpx.Response(202, request=request)):
            await webhooks.WebhookManager()._deliver(webhooks.claim_due_delivery())
        self.assertEqual(self.state(delivery), ("delivered", None, 2))

    async def test_an_address_that_is_never_allowed_is_still_refused_without_another_try(self):
        delivery = self.queue(self.receiver("inside", "https://127.0.0.1/hook"))
        await webhooks.WebhookManager()._deliver(webhooks.claim_due_delivery())
        self.assertEqual(self.state(delivery), ("failed", "invalid_destination", 1))

    def test_saving_a_receiver_whose_name_does_not_resolve_is_still_refused(self):
        def no_dns(*args, **kwargs):
            raise OSError("Name or service not known")

        with patch("app.webhooks.socket.getaddrinfo", no_dns), self.assertRaises(ValueError):
            webhooks.validate_target_url("https://alerts.example.test/hook", False)


class SideBySideTests(DeliveryCase):
    async def test_each_receiver_has_one_delivery_at_a_time_and_another_receiver_is_not_kept_waiting(self):
        dead, healthy = self.receiver("dead"), self.receiver("healthy")
        first, second, other = self.queue(dead), self.queue(dead), self.queue(healthy)
        self.assertEqual(webhooks.claim_due_delivery()["id"], first)
        self.assertEqual(webhooks.claim_due_delivery()["id"], other, "the healthy receiver waited behind the other's backlog")
        self.assertIsNone(webhooks.claim_due_delivery())
        webhooks.finish_delivery(first, 1, response_status=202)
        self.assertEqual(webhooks.claim_due_delivery()["id"], second)

    async def test_a_healthy_receivers_alert_arrives_while_another_receiver_is_not_answering(self):
        dead, healthy = self.receiver("dead", "http://198.51.100.99/hook"), self.receiver("healthy")
        for _ in range(3):
            self.queue(dead)
        alert = self.queue(healthy)
        release = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "198.51.100.99":
                await release.wait()  # Never answers while the test runs
            return httpx.Response(202, request=request)

        manager = webhooks.WebhookManager()
        with self.answering(handler):
            await manager.start()
            try:
                for _ in range(250):
                    if self.state(alert)[0] == "delivered":
                        break
                    await asyncio.sleep(0.02)
            finally:
                release.set()
                await manager.stop()
        self.assertEqual(self.state(alert)[0], "delivered")

    async def test_a_receiver_that_answers_too_slowly_is_given_up_on_and_tried_again(self):
        delivery = self.queue(self.receiver("slow"))

        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(202, request=request)

        with self.answering(handler), patch.object(webhooks, "ATTEMPT_DEADLINE_SECONDS", 0.2):
            await webhooks.WebhookManager()._deliver(webhooks.claim_due_delivery())
        status, error, _ = self.state(delivery)
        self.assertEqual((status, error), ("queued", "transport_TimeoutError"))


class HistoryTests(DeliveryCase):
    """Delivery history is pruned, and counting what is pending does not read it (#350)."""

    def plan(self, sql: str, *params: object) -> str:
        with connect() as db:
            return " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params))

    def test_finished_deliveries_older_than_the_history_period_are_removed_and_nothing_else(self):
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        endpoint = self.receiver("cloud")
        rows = {name: self.queue(endpoint) for name in ("old-delivered", "old-failed", "old-queued", "recent-delivered")}
        old, recent = (now - timedelta(days=webhooks.HISTORY_DAYS + 1)).isoformat(), (now - timedelta(days=1)).isoformat()
        with connect() as db:
            for name, status, at in (("old-delivered", "delivered", old), ("old-failed", "failed", old), ("old-queued", "queued", old), ("recent-delivered", "delivered", recent)):
                db.execute("UPDATE webhook_deliveries SET status=?,updated_at=? WHERE id=?", (status, at, rows[name]))
        self.assertEqual(webhooks.prune_deliveries(now), 2)
        with connect() as db:
            left = {row[0] for row in db.execute("SELECT id FROM webhook_deliveries")}
        self.assertEqual(left, {rows["old-queued"], rows["recent-delivered"]})
        self.assertEqual(webhooks.prune_deliveries(now), 0)

    def test_counting_a_receivers_pending_deliveries_does_not_read_its_history(self):
        plan = self.plan("SELECT COUNT(*) FROM webhook_deliveries WHERE endpoint_id=? AND status IN ('queued','delivering')", "e")
        self.assertIn("webhook_deliveries_endpoint_status_idx", plan)
        endpoint = self.receiver("busy")
        with connect() as db:
            db.execute("BEGIN")
            db.executemany("INSERT INTO webhook_deliveries(id,endpoint_id,endpoint_name,event_id,event_type,payload_json,status,attempt_count,next_attempt_at,created_at,updated_at) "
                           "VALUES(?,?,'r',?,'webhook.test','{}','delivered',1,'a','a','a')", ((f"d{number}", endpoint, f"e{number}") for number in range(20000)))
            db.execute("COMMIT")
            steps = 0

            def count() -> int:
                nonlocal steps
                steps += 1
                return 0

            db.set_progress_handler(count, 1000)
            queued = webhooks.enqueue_event(db, "webhook.test", {"event_id": "new", "event_type": "webhook.test"}, endpoint)
            db.set_progress_handler(None, 0)
        self.assertEqual(queued, 1)
        self.assertLess(steps, 20, "queueing one event read the receiver's delivery history")

    def test_the_newest_deliveries_are_listed_from_an_index(self):
        plan = self.plan("SELECT d.* FROM webhook_deliveries d ORDER BY d.created_at DESC LIMIT ?", 100)
        self.assertIn("webhook_deliveries_created_idx", plan)
        self.assertNotIn("TEMP B-TREE", plan)


if __name__ == "__main__":
    unittest.main()
