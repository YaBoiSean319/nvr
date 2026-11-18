"""Odd input on the sign-in paths is refused, not answered with a server error (#371)."""
from __future__ import annotations

import hashlib
import unittest

from fastapi.testclient import TestClient

from app import external_events, webauthn
from app.principal import WALL_COOKIE_NAME
from app.security import token_digest
from tests.test_access_grants import GrantCase

ODD = "café-\U0001f511"


class DigestTests(unittest.TestCase):
    def test_every_token_the_nvr_issues_has_the_digest_it_always_had(self):
        for token in ("abc", "nvr_AbC-123_xyz", "A" * 43):
            self.assertEqual(token_digest(token), hashlib.sha256(token.encode("ascii")).hexdigest())

    def test_a_token_that_is_not_ascii_is_digested_and_matches_nothing(self):
        self.assertEqual(len(token_digest(ODD)), 64)
        self.assertEqual(len(token_digest("\udcff")), 64)  # A lone surrogate, as a decoded header can hold

    def test_a_signature_that_is_not_ascii_is_wrong_not_an_error(self):
        self.assertFalse(external_events.verify_signature("secret", b"{}", "1700000000", "sha256=" + ODD, now=1700000000))
        self.assertFalse(external_events.verify_signature("secret", b"{}", "1700000000", "sha256=\udcff", now=1700000000))

    def test_a_passkey_answer_that_is_not_shaped_like_one_is_malformed(self):
        rp = webauthn.RelyingParty("nvr.example.test", "https://nvr.example.test")
        for credential in ({"response": "a string"}, {"response": ["a", "list"]}, {"response": None}, {}):
            with self.assertRaises(webauthn.WebAuthnError, msg=credential):
                webauthn.verify_registration(credential, b"challenge", rp)
            with self.assertRaises(webauthn.WebAuthnError, msg=credential):
                webauthn.verify_assertion(credential, b"challenge", rp, b"", 0, False)


class RouteTests(GrantCase):
    def test_a_session_or_wall_cookie_that_is_not_ascii_is_not_signed_in(self):
        for name in ("nvr_session", WALL_COOKIE_NAME):
            client = TestClient(self.app, raise_server_exceptions=False)
            response = client.get("/api/cameras", headers={"Cookie": f"{name}=café".encode("utf-8")})
            self.assertEqual(response.status_code, 401, name)

    def test_a_sign_in_challenge_that_is_not_ascii_is_an_ordinary_refusal(self):
        client = TestClient(self.app, raise_server_exceptions=False)
        for path, body in (("/api/auth/mfa/verify", {"challenge": ODD, "code": "123456"}), ("/api/auth/mfa/passkey/options", {"challenge": ODD})):
            response = client.post(path, json=body)
            self.assertIn(response.status_code, (400, 401, 422), f"{path}: {response.status_code} {response.text}")


if __name__ == "__main__":
    unittest.main()
