"""When remote sessions need a second factor, the first one cannot be set up from outside (#374).

The ticket's case: someone remote with a stolen password signs in, is held at "set up a second factor", and used to be able to set up their
own authenticator in two requests, after which the real owner was asked for a factor only the intruder held. The owner chose to allow a
first factor only from the local network when the remote rule is on."""
from __future__ import annotations

import base64
import unittest

from app import mfa
from app.api.mfa import FIRST_FACTOR_ON_SITE
from app.store import connect as database
from tests import test_remote_access
from tests.test_remote_access import OPERATOR_PASSWORD
from tests.test_support import TEST_PASSWORD


def code_for(secret_text: str) -> str:
    plain = secret_text.replace(" ", "").upper()  # Shown in groups for typing into an app
    padded = plain + "=" * (-len(plain) % 8)
    return mfa.totp_code(base64.b32decode(padded), mfa.totp_step())


class RemoteFirstFactorTests(test_remote_access.ProxiedCase):
    def setUp(self):
        super().setUp()
        self.trust("127.0.0.1")
        test_remote_access.ProxiedCase.add_operator(self)
        self.direct.post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD})
        self.assertEqual(self.direct.put("/api/admin/security", json={"mfa_policy": "off", "remote_mfa": True}).status_code, 200)

    def sign_in(self, client):
        response = client.post("/api/auth/login", json={"username": "viewer", "password": OPERATOR_PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def remote(self):
        self.proxy.client = "203.0.113.9"
        client = self.through_proxy()
        self.assertTrue(self.sign_in(client)["mfa_enrolment_required"])
        return client

    def factors(self) -> bool:
        with database() as db:
            user_id = db.execute("SELECT id FROM users WHERE username='viewer'").fetchone()[0]
            return mfa.has_factor(db, user_id)

    def test_someone_remote_with_only_the_password_cannot_set_up_a_first_factor(self):
        client = self.remote()
        for path, body in (("/api/me/security/totp/begin", {"password": OPERATOR_PASSWORD}),
                           ("/api/me/security/totp/confirm", {"code": "123456"}),
                           ("/api/me/security/passkeys/options", {"password": OPERATOR_PASSWORD})):
            with self.subTest(path):
                refused = client.post(path, json=body)
                self.assertEqual((refused.status_code, refused.json()["detail"]), (403, FIRST_FACTOR_ON_SITE))
        self.assertFalse(self.factors())
        self.assertEqual(client.get("/api/cameras").status_code, 403)  # Still held, as before
        self.assertEqual(client.get("/api/me/security").status_code, 200)  # And can still read why

    def test_on_the_local_network_the_first_factor_is_set_up_and_then_works_from_outside(self):
        local = self.local_client()
        self.addCleanup(local.close)
        self.sign_in(local)
        begun = local.post("/api/me/security/totp/begin", json={"password": OPERATOR_PASSWORD})
        self.assertEqual(begun.status_code, 200, begun.text)
        confirmed = local.post("/api/me/security/totp/confirm", json={"code": code_for(begun.json()["secret"])})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertTrue(self.factors())
        self.proxy.client = "203.0.113.9"
        remote = self.through_proxy()
        answer = self.sign_in(remote)
        self.assertNotIn("mfa_enrolment_required", answer)  # Now asked for the factor it has, as any protected account
        self.assertTrue(answer.get("mfa_required"), answer)

    def test_without_the_remote_rule_nothing_changes(self):
        self.assertEqual(self.direct.put("/api/admin/security", json={"remote_mfa": False}).status_code, 200)
        self.proxy.client = "203.0.113.9"
        client = self.through_proxy()
        self.sign_in(client)
        self.assertEqual(client.post("/api/me/security/totp/begin", json={"password": OPERATOR_PASSWORD}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
