from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import digests, email_outbox, heartbeat, push, rule_actions, webhooks
from app.store import connect, initialize


class IdleClaimTests(unittest.TestCase):
    """A background loop that finds nothing queued must not take SQLite's one write lock; it did, every second (#280)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        initialize()

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.temporary.cleanup()

    def statements(self, call):
        seen: list[str] = []
        real = sqlite3.connect

        def watching(*args, **kwargs):
            connection = real(*args, **kwargs)
            connection.set_trace_callback(seen.append)
            return connection

        with patch("sqlite3.connect", watching):
            result = call()
        return result, seen

    def test_a_device_secret_that_matches_nothing_takes_no_write_lock(self):
        from app import device_sessions
        result, seen = self.statements(lambda: device_sessions.resume(device_sessions.PREFIX + "A" * 43))
        self.assertIsNone(result)
        self.assertFalse([statement for statement in seen if statement.upper().startswith(("BEGIN", "INSERT", "UPDATE", "DELETE"))], seen)

    def test_the_delivery_history_is_pruned_once_an_hour_not_after_every_delivery(self):
        with patch.object(rule_actions, "_pruned_at", None):
            _, first = self.statements(lambda: rule_actions._finish("no-such-delivery", 1, response_status=200))
            _, second = self.statements(lambda: rule_actions._finish("no-such-delivery", 1, response_status=200))
        prunes = [sum(1 for statement in seen if statement.startswith("DELETE FROM rule_action_deliveries")) for seen in (first, second)]
        self.assertEqual(prunes, [1, 0])

    def test_nothing_queued_takes_no_write_lock(self):
        def claim_push():
            with connect() as db:
                return push._claim(db)

        for name, claim in (("webhooks", webhooks.claim_due_delivery), ("heartbeat", heartbeat.claim_due_heartbeat), ("email", email_outbox._claim),
                            ("digests", digests._claim), ("rule actions", rule_actions._claim), ("push", claim_push)):
            with self.subTest(name):
                result, seen = self.statements(claim)
                self.assertIsNone(result)
                self.assertEqual([statement for statement in seen if statement.lstrip().upper().startswith("BEGIN")], [], seen)

    def test_queued_work_is_still_claimed(self):
        with connect() as db:
            db.execute("INSERT INTO email_outbox(id,kind,subject,body,recipients_json,next_attempt_at,created_at,updated_at) VALUES('m1','incident','s','b','[]','2000-01-01T00:00:00+00:00','2000-01-01T00:00:00+00:00','2000-01-01T00:00:00+00:00')")
        claimed, seen = self.statements(email_outbox._claim)
        self.assertEqual(claimed["id"], "m1")
        self.assertTrue(any(statement.lstrip().upper().startswith("BEGIN") for statement in seen))
        self.assertIsNone(email_outbox._claim(), "claimed once")

    # More loops and refusals that took the lock with nothing to change (#353).

    WRITES = ("BEGIN", "INSERT", "UPDATE", "DELETE")

    def writes(self, seen: list[str]) -> list[str]:
        return [statement for statement in seen if statement.lstrip().upper().startswith(self.WRITES)]

    def test_more_idle_passes_take_no_write_lock(self):
        from datetime import datetime, timezone

        from app import failover, redaction

        for name, call in (("held-email summary", email_outbox.queue_held_summary), ("email delivery", email_outbox.deliver_one),
                           ("redaction reviews", redaction.expire_reviews), ("standby takeover check", lambda: failover.check_takeover(datetime.now(timezone.utc)))):
            with self.subTest(name):
                _, seen = self.statements(call)
                self.assertEqual(self.writes(seen), [], name)

    def test_a_ptz_tour_at_rest_writes_its_state_once_not_on_every_pass(self):
        from datetime import datetime, timezone

        from app import ptz

        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('c1','Yard',X'01')")
            db.execute("INSERT INTO ptz_tours(id,camera_id,name,steps_json,enabled) VALUES('t1','c1','Night round','[]',1)")
        now = datetime.now(timezone.utc)
        _, first = self.statements(lambda: ptz._due_moves(now))
        self.assertEqual(len(self.writes(first)), 1)
        for _ in range(3):
            moves, seen = self.statements(lambda: ptz._due_moves(now))
            self.assertEqual((moves, self.writes(seen)), ([], []))

    def test_a_sign_in_attempt_that_is_already_waiting_is_refused_without_the_lock(self):
        from fastapi import HTTPException

        from app import auth_limits

        for _ in range(8):
            try:
                auth_limits.begin("admin", "198.51.100.7")
            except HTTPException:
                break

        def attempt():
            with self.assertRaises(HTTPException) as caught:
                auth_limits.begin("admin", "198.51.100.7")
            return caught.exception.status_code

        status, seen = self.statements(attempt)
        self.assertEqual((status, self.writes(seen)), (429, []))


if __name__ == "__main__":
    unittest.main()
