"""A primary yields only to an answer it can prove came from its standby, and not for ever (#343).

Requests between a primary and its standby were signed and answers were not: anything able to alter the traffic could answer a renewal
"not granted", the primary stopped recording, the idle time was counted as expected, and the standby, still hearing renewals, never took
over. Nothing recorded."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

from app import failover
from app.store import connect
from tests.test_failover import SECRET, FailoverCase

PATH = "/api/inbound/failover/lease"
NOT_GRANTED = json.dumps({"granted": False, "state": "stopping"}).encode()
GRANTED = json.dumps({"granted": True, "state": "watching", "epoch": 0, "handback": None}).encode()


class AnswerCase(FailoverCase):
    def setUp(self):
        super().setUp()
        self.configure("primary")
        failover.FailoverManager._apply_answer({"lease_seconds": 10}, datetime.now(timezone.utc), {"granted": True, "epoch": 0, "handback": None})  # Recording, lease held
        self.assertIsNone(self.policy().get("held"))

    def settings(self) -> dict:
        with connect() as db:
            return failover.read_settings(db)

    def renew(self, body: bytes, headers=None, status: int = 200) -> dict:
        """One renewal by the primary, answered with these bytes and headers. ``headers`` may be a function of the request's signature."""
        request_signature = "sha256=" + "a" * 64

        async def call(*_args, **_kwargs):
            answer_headers = headers(request_signature) if callable(headers) else (headers or {})
            response = httpx.Response(status, content=body, headers={"content-type": "application/json", **answer_headers})
            response.extensions["failover_request_signature"] = request_signature
            return response

        manager = failover.FailoverManager()
        with mock.patch.object(manager, "_call", call):
            asyncio.run(manager._renew(self.settings(), SECRET))
        return self.state()


class SignedAnswerTests(AnswerCase):
    def test_a_forged_not_granted_answer_does_not_stop_the_primary_recording(self):
        state = self.renew(NOT_GRANTED)  # What someone in the middle sends: unsigned
        self.assertIsNone(self.policy().get("held"), "an unsigned answer made the primary stop recording")
        self.assertEqual(state["peer_state"], "unverified")
        self.assertIn("not signed", state["last_error"])

    def test_an_answer_signed_with_another_secret_or_altered_after_signing_is_no_answer(self):
        wrong = self.renew(NOT_GRANTED, lambda request: failover.sign_answer("some-other-secret-0123456789", PATH, NOT_GRANTED, request))
        self.assertEqual((wrong["peer_state"], self.policy().get("held")), ("unverified", None))
        altered = self.renew(NOT_GRANTED, lambda request: failover.sign_answer(SECRET, PATH, GRANTED, request))  # A genuine "granted", its body swapped
        self.assertEqual((altered["peer_state"], self.policy().get("held")), ("unverified", None))

    def test_an_answer_to_an_earlier_request_cannot_be_replayed(self):
        earlier = failover.sign_answer(SECRET, PATH, NOT_GRANTED, "sha256=" + "b" * 64)  # Genuine, for another renewal
        self.assertEqual(self.renew(NOT_GRANTED, earlier)["peer_state"], "unverified")
        stale = self.renew(NOT_GRANTED, lambda request: failover.sign_answer(SECRET, PATH, NOT_GRANTED, request, timestamp=1_000_000))
        self.assertEqual((stale["peer_state"], self.policy().get("held")), ("unverified", None))

    def test_the_standbys_own_signed_answers_are_obeyed(self):
        yielded = self.renew(NOT_GRANTED, lambda request: failover.sign_answer(SECRET, PATH, NOT_GRANTED, request))
        self.assertEqual((yielded["peer_state"], self.policy()["held"]), ("standby_stopping", "failover_yield"))
        granted = self.renew(GRANTED, lambda request: failover.sign_answer(SECRET, PATH, GRANTED, request))
        self.assertEqual((granted["peer_state"], self.policy().get("held")), ("ok", None))

    def test_the_standby_signs_what_it_answers(self):
        from fastapi.testclient import TestClient
        self.configure("standby")
        body = b"{}"
        headers = failover.sign(SECRET, "POST", PATH, body)
        answer = TestClient(self.app).post(PATH, content=body, headers=headers)
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertTrue(failover.verify_answer(SECRET, PATH, answer.content, headers["X-Failover-Signature"], answer.headers["x-failover-timestamp"], answer.headers["x-failover-signature"]))
        self.assertFalse(failover.verify_answer(SECRET, PATH, answer.content, "sha256=" + "c" * 64, answer.headers["x-failover-timestamp"], answer.headers["x-failover-signature"]))


class YieldLimitTests(AnswerCase):
    def not_yet(self) -> dict:
        return self.renew(NOT_GRANTED, lambda request: failover.sign_answer(SECRET, PATH, NOT_GRANTED, request))

    def test_a_standby_that_never_hands_back_does_not_keep_the_primary_from_recording_for_ever(self):
        self.assertEqual(self.not_yet()["peer_state"], "standby_stopping")
        self.assertEqual(self.policy()["held"], "failover_yield")
        self.assertIsNone(self.incident())
        with connect() as db:  # The "not yet" answers have gone on past the limit
            db.execute("UPDATE failover_state SET yield_since=?", ((datetime.now(timezone.utc) - timedelta(seconds=failover.YIELD_LIMIT_SECONDS + 5)).isoformat(),))
        state = self.not_yet()
        self.assertEqual(state["peer_state"], "not_handing_back")
        self.assertIsNone(self.policy().get("held"), "the primary is still not recording")
        with connect() as db:
            incident = db.execute("SELECT summary,fault_active FROM incidents WHERE incident_key=?", (failover.YIELD_KEY,)).fetchone()
        self.assertEqual(incident["fault_active"], 1)
        self.assertIn("did not hand recording back", incident["summary"])
        self.assertIsNone(self.not_yet().get("yield_until"))  # And it stays recording while the answers go on

    def test_the_incident_recovers_when_the_standby_grants(self):
        self.not_yet()
        with connect() as db:
            db.execute("UPDATE failover_state SET yield_since=?", ((datetime.now(timezone.utc) - timedelta(seconds=failover.YIELD_LIMIT_SECONDS + 5)).isoformat(),))
        self.not_yet()
        granted = self.renew(GRANTED, lambda request: failover.sign_answer(SECRET, PATH, GRANTED, request))
        self.assertEqual((granted["peer_state"], granted["yield_since"]), ("ok", None))
        with connect() as db:
            self.assertEqual(db.execute("SELECT fault_active FROM incidents WHERE incident_key=?", (failover.YIELD_KEY,)).fetchone()[0], 0)

    def test_a_short_handback_is_not_an_incident_and_the_count_starts_again_after_it(self):
        self.not_yet()
        first = self.state()["yield_since"]
        self.not_yet()
        self.assertEqual(self.state()["yield_since"], first)  # One run of "not yet", counted from its first
        self.renew(GRANTED, lambda request: failover.sign_answer(SECRET, PATH, GRANTED, request))
        self.assertIsNone(self.state()["yield_since"])
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM incidents WHERE incident_key=?", (failover.YIELD_KEY,)).fetchone())
