"""Requests are limited in size before sign-in (#369), and one that changes something is refused when it comes from a page on another site (#370)."""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import request_guards
from tests.test_access_grants import GrantCase

MIB = 1024 * 1024


class SizeLimitTests(GrantCase):
    def anonymous(self) -> TestClient:
        return TestClient(self.app, raise_server_exceptions=False)

    def test_a_declared_length_over_the_limit_is_refused_before_sign_in(self):
        response = self.anonymous().post("/api/auth/login", content=b"x" * (request_guards.DEFAULT_MAX_BODY_BYTES + 1), headers={"Content-Type": "application/json"})
        self.assertEqual((response.status_code, response.json()), (413, {"detail": "The request is too large"}))

    def test_a_body_sent_without_a_length_is_cut_off_at_the_limit_and_not_held(self):
        """Driven as the server drives it, a piece at a time: the route is given no more than the limit, and the answer is 413 whatever it says."""
        import asyncio

        taken = {"pieces": 0}
        got = {"bytes": 0}
        sent: list[dict] = []

        async def route(scope, receive, send):  # Reads its whole body, as the framework does before sign-in
            while True:
                message = await receive()
                got["bytes"] += len(message.get("body") or b"")
                if message["type"] != "http.request" or not message.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def receive():
            taken["pieces"] += 1
            return {"type": "http.request", "body": b"x" * MIB, "more_body": True}  # For ever

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/api/cameras/bulk-jobs", "headers": [(b"content-type", b"application/json")]}
        asyncio.run(request_guards.RequestSizeLimitMiddleware(route)(scope, receive, send))
        self.assertEqual([message.get("status") for message in sent if message["type"] == "http.response.start"], [413])
        self.assertLessEqual(taken["pieces"], request_guards.DEFAULT_MAX_BODY_BYTES // MIB + 1)
        self.assertLessEqual(got["bytes"], request_guards.DEFAULT_MAX_BODY_BYTES)

    def test_an_ordinary_request_is_untouched_signed_in_or_not(self):
        self.assertEqual(self.anonymous().post("/api/auth/login", json={"username": "nobody", "password": "wrong-password-1"}).status_code, 401)
        self.assertEqual(self.admin.get("/api/cameras").status_code, 200)

    def test_the_limit_can_be_set_and_has_a_floor(self):
        with patch.dict(os.environ, {"NVR_MAX_REQUEST_BYTES": "100000"}):
            self.assertEqual(request_guards.max_body_bytes(), 100000)
            self.assertEqual(self.anonymous().post("/api/auth/login", content=b"x" * 100001, headers={"Content-Type": "application/json"}).status_code, 413)
        with patch.dict(os.environ, {"NVR_MAX_REQUEST_BYTES": "5"}):
            self.assertEqual(request_guards.max_body_bytes(), 64 * 1024)
        with patch.dict(os.environ, {"NVR_MAX_REQUEST_BYTES": "lots"}):
            self.assertEqual(request_guards.max_body_bytes(), request_guards.DEFAULT_MAX_BODY_BYTES)

    def test_the_evidence_upload_routes_keep_their_own_larger_limit(self):
        for path in ("/api/exports/job-1/verify", "/api/cases/case-1/packages/pkg-1/verify"):
            self.assertIsNotNone(request_guards._OWN_LIMIT.match(path), path)
            response = self.anonymous().post(path, content=b"x" * (request_guards.DEFAULT_MAX_BODY_BYTES + 1))
            self.assertNotEqual(response.status_code, 413, path)  # Not this check's refusal: the route's own sign-in and limits apply
        self.assertIsNone(request_guards._OWN_LIMIT.match("/api/exports/job-1/verify/extra"))


class SameOriginTests(GrantCase):
    def test_a_change_from_a_page_on_another_site_is_refused(self):
        response = self.admin.post("/api/hardening/run", headers={"Origin": "https://wiki.example.test"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("another site", response.json()["detail"])
        self.assertEqual(self.admin.post("/api/auth/logout", headers={"Origin": "null"}).status_code, 403)

    def test_the_nvrs_own_pages_and_callers_without_an_origin_are_not_affected(self):
        own = self.admin.post("/api/auth/login", json={"username": "nobody", "password": "wrong-password-1"}, headers={"Origin": "http://testserver"})
        self.assertEqual(own.status_code, 401)  # Reached the route, which refused the password
        self.assertEqual(self.admin.post("/api/auth/login", json={"username": "nobody", "password": "wrong-password-1"}).status_code, 401)

    def test_reading_is_never_refused_for_its_origin(self):
        self.assertEqual(self.admin.get("/api/cameras", headers={"Origin": "https://wiki.example.test"}).status_code, 200)

    def test_an_allowed_origin_and_the_public_address_may_make_changes(self):
        attempt = {"username": "nobody", "password": "wrong-password-1"}
        with patch.dict(os.environ, {"NVR_ALLOWED_ORIGINS": "https://console.example.test", "NVR_PUBLIC_URL": "https://nvr.example.test"}):
            for origin in ("https://console.example.test", "https://nvr.example.test"):
                self.assertEqual(self.admin.post("/api/auth/login", json=attempt, headers={"Origin": origin}).status_code, 401, origin)
            self.assertEqual(self.admin.post("/api/auth/login", json=attempt, headers={"Origin": "https://other.example.test"}).status_code, 403)

    def test_the_signed_routes_other_systems_call_are_not_checked_for_origin(self):
        response = self.admin.post("/api/inbound/events/no-such-source", content=b"{}", headers={"Origin": "https://door-controller.example.test"})
        self.assertEqual(response.status_code, 401)  # Refused for its signature, not its origin
