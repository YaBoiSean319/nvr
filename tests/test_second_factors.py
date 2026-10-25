"""Passkeys, TOTP and recovery codes for local accounts, the policy, rate limiting and the audit trail (#118)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import auth_limits, mfa
from app.application import create_app
from app.security import hash_password, new_session_token, token_digest
from app.store import connect
from tests.fixtures.software_authenticator import SoftwareAuthenticator, decode_secret
from tests.test_support import TEST_PASSWORD

OPERATOR_PASSWORD = "operator-password-1234"


class SecondFactorCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.environment = {"NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_RECORDINGS_PATH": str(root / "recordings"),
                            "NVR_CREDENTIAL_KEY_PATH": str(root / "credentials.key")}
        self.previous = {key: os.environ.get(key) for key in (*self.environment, "NVR_PUBLIC_URL", "NVR_WEBAUTHN_RP_ID")}
        os.environ.update(self.environment)
        os.environ.pop("NVR_PUBLIC_URL", None)
        os.environ.pop("NVR_WEBAUTHN_RP_ID", None)
        (root / "recordings").mkdir()
        self.app = create_app(start_workers=False)
        self.context = TestClient(self.app)
        self.admin = self.context.__enter__()
        created = self.admin.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD})
        self.assertEqual(created.status_code, 201, created.text)

    def tearDown(self):
        self.context.__exit__(None, None, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def browser(self) -> TestClient:
        """A new client with no cookies, as another person's browser."""
        return TestClient(self.app)

    def add_operator(self, name: str = "operator") -> int:
        with connect() as db:
            return int(db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,'operator')", (name, hash_password(OPERATOR_PASSWORD))).lastrowid)

    def audit(self, action: str) -> list[dict]:
        with connect() as db:
            return [{**dict(row), "details": json.loads(row["details_json"])} for row in db.execute("SELECT * FROM audit_log WHERE action=? ORDER BY id", (action,))]

    def sign_in(self, client: TestClient, username: str = "keeper", password: str = TEST_PASSWORD) -> dict:
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def enrol_totp(self, client: TestClient) -> tuple[bytes, list[str]]:
        begun = client.post("/api/me/security/totp/begin")
        self.assertEqual(begun.status_code, 200, begun.text)
        secret = decode_secret(begun.json()["secret"])
        confirmed = client.post("/api/me/security/totp/confirm", json={"code": mfa.totp_code(secret, mfa.totp_step())})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        return secret, confirmed.json()["recovery_codes"]

    def enrol_passkey(self, client: TestClient, authenticator: SoftwareAuthenticator, name: str = "Laptop") -> dict:
        options = client.post("/api/me/security/passkeys/options").json()
        registered = client.post("/api/me/security/passkeys", json={"credential": authenticator.create(options), "name": name})
        self.assertEqual(registered.status_code, 201, registered.text)
        return registered.json()


class TotpTests(SecondFactorCase):
    def test_enrolling_totp_then_signing_in_with_a_code(self):
        secret, codes = self.enrol_totp(self.admin)
        self.assertEqual(len(codes), 10)
        other = self.browser()
        challenge = self.sign_in(other)
        self.assertTrue(challenge["mfa_required"])
        self.assertEqual(challenge["methods"], ["totp", "recovery"])
        self.assertEqual(other.get("/api/auth/me").status_code, 401)  # The password alone gives no session.

        wrong = other.post("/api/auth/mfa/verify", json={"challenge": challenge["challenge"], "method": "totp", "code": "000000"})
        self.assertEqual(wrong.status_code, 401)
        step = mfa.totp_step() + 1  # The next window: valid, and not the one enrolment used.
        right = other.post("/api/auth/mfa/verify", json={"challenge": challenge["challenge"], "method": "totp", "code": mfa.totp_code(secret, step)})
        self.assertEqual(right.status_code, 200, right.text)
        self.assertEqual(right.json()["username"], "keeper")
        self.assertEqual(other.get("/api/auth/me").json()["username"], "keeper")

    def test_a_code_works_once_and_a_challenge_works_once(self):
        secret, _ = self.enrol_totp(self.admin)
        code = mfa.totp_code(secret, mfa.totp_step() + 1)
        first = self.browser().post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD}).json()["challenge"]
        self.assertEqual(self.browser().post("/api/auth/mfa/verify", json={"challenge": first, "method": "totp", "code": code}).status_code, 200)
        second = self.browser().post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD}).json()["challenge"]
        replay = self.browser().post("/api/auth/mfa/verify", json={"challenge": second, "method": "totp", "code": code})
        self.assertEqual(replay.status_code, 401)  # A code is spent when used.
        self.assertEqual(self.browser().post("/api/auth/mfa/verify", json={"challenge": first, "method": "totp", "code": mfa.totp_code(secret, mfa.totp_step() + 2)}).status_code, 401)

    def test_a_challenge_is_burned_after_five_wrong_answers(self):
        secret, _ = self.enrol_totp(self.admin)
        client = self.browser()
        challenge = self.sign_in(client)["challenge"]
        for _ in range(5):
            self.assertEqual(client.post("/api/auth/mfa/verify", json={"challenge": challenge, "method": "totp", "code": "111111"}).status_code, 401)
        good = mfa.totp_code(secret, mfa.totp_step() + 1)
        self.assertEqual(client.post("/api/auth/mfa/verify", json={"challenge": challenge, "method": "totp", "code": good}).status_code, 401)

    def test_enrolling_needs_the_right_code(self):
        self.admin.post("/api/me/security/totp/begin")
        wrong = self.admin.post("/api/me/security/totp/confirm", json={"code": "123456"})
        self.assertEqual(wrong.status_code, 422)
        self.assertFalse(self.admin.get("/api/me/security").json()["totp"])

    def test_removing_totp_asks_for_the_password(self):
        self.enrol_totp(self.admin)
        refused = self.admin.request("DELETE", "/api/me/security/totp", json={"password": "not the password 123"})
        self.assertEqual(refused.status_code, 403)
        self.assertTrue(self.admin.get("/api/me/security").json()["totp"])
        removed = self.admin.request("DELETE", "/api/me/security/totp", json={"password": TEST_PASSWORD})
        self.assertEqual(removed.status_code, 200)
        summary = removed.json()
        self.assertEqual((summary["totp"], summary["recovery_codes_remaining"]), (False, 0))
        self.assertNotIn("mfa_required", self.sign_in(self.browser()))


class RecoveryCodeTests(SecondFactorCase):
    def test_each_recovery_code_works_once(self):
        _, codes = self.enrol_totp(self.admin)
        client = self.browser()
        challenge = self.sign_in(client)["challenge"]
        used = client.post("/api/auth/mfa/verify", json={"challenge": challenge, "method": "recovery", "code": codes[0]})
        self.assertEqual(used.status_code, 200, used.text)
        self.assertEqual(self.admin.get("/api/me/security").json()["recovery_codes_remaining"], 9)
        again = self.browser()
        refused = again.post("/api/auth/mfa/verify", json={"challenge": self.sign_in(again)["challenge"], "method": "recovery", "code": codes[0]})
        self.assertEqual(refused.status_code, 401)
        other = self.browser()  # Another code still works, typed in a different form.
        accepted = other.post("/api/auth/mfa/verify", json={"challenge": self.sign_in(other)["challenge"], "method": "recovery", "code": codes[1].upper().replace("-", " ")})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual([entry["details"]["method"] for entry in self.audit("mfa_recovery_code_used")], ["recovery", "recovery"])

    def test_regenerating_replaces_every_old_code_and_needs_the_password(self):
        _, codes = self.enrol_totp(self.admin)
        self.assertEqual(self.admin.post("/api/me/security/recovery-codes", json={"password": "wrong password 12345"}).status_code, 403)
        fresh = self.admin.post("/api/me/security/recovery-codes", json={"password": TEST_PASSWORD}).json()["recovery_codes"]
        self.assertEqual(len(set(fresh) & set(codes)), 0)
        client = self.browser()
        old = client.post("/api/auth/mfa/verify", json={"challenge": self.sign_in(client)["challenge"], "method": "recovery", "code": codes[0]})
        self.assertEqual(old.status_code, 401)
        client = self.browser()
        new = client.post("/api/auth/mfa/verify", json={"challenge": self.sign_in(client)["challenge"], "method": "recovery", "code": fresh[0]})
        self.assertEqual(new.status_code, 200)

    def test_codes_are_stored_only_as_digests(self):
        _, codes = self.enrol_totp(self.admin)
        with connect() as db:
            stored = [row[0] for row in db.execute("SELECT code_hash FROM user_recovery_codes")]
        self.assertEqual(len(stored), 10)
        self.assertFalse(any(code.replace("-", "") in hash_ for code in codes for hash_ in stored))


class PasskeyTests(SecondFactorCase):
    def test_a_passkey_enrols_and_answers_the_second_step(self):
        authenticator = SoftwareAuthenticator()
        summary = self.enrol_passkey(self.admin, authenticator)
        self.assertEqual([item["name"] for item in summary["passkeys"]], ["Laptop"])
        self.assertEqual(len(summary["recovery_codes"]), 10)

        client = self.browser()
        challenge = self.sign_in(client)
        self.assertEqual(challenge["methods"], ["passkey", "recovery"])
        options = client.post("/api/auth/mfa/passkey/options", json={"challenge": challenge["challenge"]}).json()
        self.assertEqual(options["allowCredentials"][0]["id"], summary["passkeys"] and options["allowCredentials"][0]["id"])
        signed = client.post("/api/auth/mfa/passkey/verify", json={"challenge": challenge["challenge"], "credential": authenticator.get(options)})
        self.assertEqual(signed.status_code, 200, signed.text)
        self.assertEqual(client.get("/api/auth/me").json()["username"], "keeper")
        with connect() as db:
            self.assertEqual(db.execute("SELECT sign_count FROM user_passkeys").fetchone()[0], 1)

    def second_step(self, authenticator: SoftwareAuthenticator, **assertion) -> tuple[TestClient, object]:
        client = self.browser()
        challenge = self.sign_in(client)["challenge"]
        options = client.post("/api/auth/mfa/passkey/options", json={"challenge": challenge}).json()
        return client, client.post("/api/auth/mfa/passkey/verify", json={"challenge": challenge, "credential": authenticator.get(options, **assertion)})

    def test_forged_and_misdirected_responses_are_refused(self):
        authenticator = SoftwareAuthenticator()
        self.enrol_passkey(self.admin, authenticator)
        stranger = SoftwareAuthenticator()
        stranger.credential_id = authenticator.credential_id  # Same id, a different private key.
        self.assertEqual(self.second_step(stranger)[1].status_code, 401)
        self.assertEqual(self.second_step(authenticator, origin="http://evil.example")[1].status_code, 401)
        self.assertEqual(self.second_step(authenticator, present=False)[1].status_code, 401)
        # A counter that goes backwards means a copied authenticator.
        self.assertEqual(self.second_step(authenticator, sign_count=5)[1].status_code, 200)
        self.assertEqual(self.second_step(authenticator, sign_count=3)[1].status_code, 401)
        self.assertEqual([entry["details"]["method"] for entry in self.audit("mfa_failed")][:1], ["passkey"])

    def test_a_response_for_another_challenge_or_site_is_refused(self):
        authenticator = SoftwareAuthenticator()
        options = self.admin.post("/api/me/security/passkeys/options").json()
        wrong_site = SoftwareAuthenticator(rp_id="evil.example")
        wrong_site.credential_id = authenticator.credential_id
        refused = self.admin.post("/api/me/security/passkeys", json={"credential": wrong_site.create(options), "name": "x"})
        self.assertEqual(refused.status_code, 422)
        # The challenge was spent by that attempt.
        options = self.admin.post("/api/me/security/passkeys/options").json()
        stale = {**options, "challenge": "AAAA"}
        self.assertEqual(self.admin.post("/api/me/security/passkeys", json={"credential": authenticator.create(stale), "name": "x"}).status_code, 422)
        self.assertEqual(self.admin.get("/api/me/security").json()["passkeys"], [])

    def test_a_passkey_alone_signs_in_only_for_people_who_turned_it_on(self):
        authenticator = SoftwareAuthenticator()
        self.enrol_passkey(self.admin, authenticator)

        def attempt(**assertion):
            client = self.browser()
            started = client.post("/api/auth/passkey/options").json()
            return client, client.post("/api/auth/passkey/login", json={"challenge": started["challenge"], "credential": authenticator.get(started["options"], **assertion)})

        refused = attempt()[1]
        self.assertEqual(refused.status_code, 401)
        self.assertIn("turned off", refused.json()["detail"])

        self.assertEqual(self.admin.put("/api/me/security/passkey-sign-in", json={"enabled": True, "password": "wrong password 12345"}).status_code, 403)
        self.assertEqual(self.admin.put("/api/me/security/passkey-sign-in", json={"enabled": True, "password": TEST_PASSWORD}).status_code, 200)

        self.assertEqual(attempt(user_verified=False)[1].status_code, 401)  # Alone, it must have verified the person.
        client, signed = attempt()
        self.assertEqual(signed.status_code, 200, signed.text)
        self.assertEqual(client.get("/api/auth/me").json()["username"], "keeper")
        self.assertEqual([entry["details"]["method"] for entry in self.audit("mfa_used")], ["passkey_sign_in"])

    def test_passkey_sign_in_needs_a_passkey_and_ends_with_the_last_one(self):
        self.assertEqual(self.admin.put("/api/me/security/passkey-sign-in", json={"enabled": True, "password": TEST_PASSWORD}).status_code, 409)
        authenticator = SoftwareAuthenticator()
        summary = self.enrol_passkey(self.admin, authenticator)
        self.admin.put("/api/me/security/passkey-sign-in", json={"enabled": True, "password": TEST_PASSWORD})
        removed = self.admin.request("DELETE", f"/api/me/security/passkeys/{summary['passkeys'][0]['id']}", json={"password": TEST_PASSWORD})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual((removed.json()["passkey_sign_in"], removed.json()["recovery_codes_remaining"]), (False, 0))

    def test_a_passkey_cannot_be_registered_twice(self):
        authenticator = SoftwareAuthenticator()
        self.enrol_passkey(self.admin, authenticator)
        options = self.admin.post("/api/me/security/passkeys/options").json()
        self.assertEqual(self.admin.post("/api/me/security/passkeys", json={"credential": authenticator.create(options), "name": "again"}).status_code, 409)

    def test_the_relying_party_follows_the_public_address_when_set(self):
        os.environ["NVR_PUBLIC_URL"] = "https://nvr.example.com"
        options = self.admin.post("/api/me/security/passkeys/options").json()
        self.assertEqual(options["rp"]["id"], "nvr.example.com")
        authenticator = SoftwareAuthenticator(rp_id="nvr.example.com", origin="https://nvr.example.com")
        registered = self.admin.post("/api/me/security/passkeys", json={"credential": authenticator.create(options), "name": "Key"})
        self.assertEqual(registered.status_code, 201, registered.text)


class PolicyTests(SecondFactorCase):
    def set_policy(self, value: str) -> None:
        response = self.admin.put("/api/admin/security", json={"mfa_policy": value})
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_required_user_without_a_factor_can_only_enrol(self):
        self.set_policy("administrators")
        client = self.browser()
        result = self.sign_in(client)
        self.assertTrue(result.get("mfa_enrolment_required", False))
        self.assertTrue(client.get("/api/auth/me").json().get("mfa_enrolment_required", False))
        for path in ("/api/cameras", "/api/settings/display", "/api/admin/sessions", "/api/live/views"):
            refused = client.get(path)
            self.assertEqual(refused.status_code, 403, path)
            self.assertIn("second factor", refused.json()["detail"])
        self.assertEqual(client.get("/api/me/security").json()["required"], True)

        self.enrol_totp(client)
        self.assertFalse(client.get("/api/auth/me").json().get("mfa_enrolment_required", False))
        self.assertEqual(client.get("/api/cameras").status_code, 200)

    def test_the_policy_decides_who_is_asked(self):
        self.add_operator()
        self.enrol_totp(self.admin)  # An administrator who sets a policy without a factor of their own must enrol first.
        self.set_policy("administrators")
        self.assertFalse(self.sign_in(self.browser(), "operator", OPERATOR_PASSWORD).get("mfa_enrolment_required", False))
        self.set_policy("all")
        client = self.browser()
        self.assertTrue(self.sign_in(client, "operator", OPERATOR_PASSWORD).get("mfa_enrolment_required", False))
        self.set_policy("off")  # Lifting the policy frees sessions already open.
        self.assertEqual(client.get("/api/cameras").status_code, 200)

    def test_a_session_already_open_is_asked_when_the_policy_arrives(self):
        self.set_policy("all")
        self.assertEqual(self.admin.get("/api/cameras").status_code, 403)
        self.assertTrue(self.admin.get("/api/auth/me").json().get("mfa_enrolment_required", False))
        self.assertEqual(self.admin.get("/api/admin/security").status_code, 403)  # Even the administrator enrols first.

    def test_entra_accounts_are_never_asked(self):
        self.set_policy("all")
        token = new_session_token()
        with connect() as db:
            user_id = db.execute("INSERT INTO users(username,password_hash,role,provider_issuer,provider_subject) VALUES('entra-user',?, 'operator', 'https://login.microsoftonline.com/x/v2.0', 's')",
                                 (hash_password("unused-password-1234"),)).lastrowid
            db.execute("INSERT INTO sessions(session_id,token_hash,user_id,expires_at) VALUES('s',?,?,?)",
                       (token_digest(token), user_id, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))
        client = self.browser()
        client.cookies.set("nvr_session", token)
        self.assertFalse(client.get("/api/auth/me").json().get("mfa_enrolment_required", False))
        self.assertEqual(client.get("/api/cameras").status_code, 200)
        summary = client.get("/api/me/security").json()
        self.assertFalse(summary["local"])
        self.assertEqual(client.post("/api/me/security/totp/begin").status_code, 409)

    def test_a_signed_in_person_with_a_factor_is_not_asked_again(self):
        self.enrol_totp(self.admin)
        self.set_policy("all")
        self.assertEqual(self.admin.get("/api/cameras").status_code, 200)

    def test_only_administrators_change_the_policy(self):
        self.add_operator()
        operator = self.browser()
        self.sign_in(operator, "operator", OPERATOR_PASSWORD)
        self.assertEqual(operator.put("/api/admin/security", json={"mfa_policy": "all"}).status_code, 403)
        self.assertEqual(self.admin.put("/api/admin/security", json={"mfa_policy": "sometimes"}).status_code, 422)
        self.set_policy("administrators")
        self.assertEqual([(entry["details"]["before"], entry["details"]["after"]) for entry in self.audit("mfa_policy_changed")], [("off", "administrators")])


class ResetTests(SecondFactorCase):
    def test_an_administrator_resets_someones_factors_and_it_is_audited(self):
        operator_id = self.add_operator()
        operator = self.browser()
        self.sign_in(operator, "operator", OPERATOR_PASSWORD)
        self.enrol_totp(operator)
        self.enrol_passkey(operator, SoftwareAuthenticator())
        listed = {user["username"]: user for user in self.admin.get("/api/admin/users").json()["users"]}
        self.assertEqual((listed["operator"]["totp"], listed["operator"]["passkeys"]), (True, 1))
        self.assertTrue(listed["operator"]["local"])

        self.assertEqual(self.admin.delete(f"/api/admin/users/{operator_id}/mfa").status_code, 204)
        self.assertEqual(operator.get("/api/auth/me").status_code, 401)  # Their sessions ended.
        self.assertNotIn("mfa_required", self.sign_in(self.browser(), "operator", OPERATOR_PASSWORD))
        reset = self.audit("mfa_reset")
        self.assertEqual((reset[0]["resource_id"], reset[0]["details"]["had_totp"], reset[0]["details"]["passkeys"]), (str(operator_id), True, 1))
        with connect() as db:
            self.assertEqual(db.execute("SELECT actor_id FROM audit_log WHERE action='mfa_reset'").fetchone()[0], 1)

    def test_resetting_is_for_administrators_and_not_yourself(self):
        operator_id = self.add_operator()
        operator = self.browser()
        self.sign_in(operator, "operator", OPERATOR_PASSWORD)
        self.assertEqual(operator.delete("/api/admin/users/1/mfa").status_code, 403)
        self.assertEqual(operator.get("/api/admin/users").status_code, 403)
        self.assertEqual(self.admin.delete("/api/admin/users/1/mfa").status_code, 409)
        self.assertEqual(self.admin.delete("/api/admin/users/999/mfa").status_code, 404)
        self.assertEqual(operator_id, 2)

    def test_the_host_can_reset_the_only_administrator(self):
        self.enrol_totp(self.admin)
        run = subprocess.run([sys.executable, "-m", "app.mfa", "reset", "keeper"], env={**os.environ}, capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("mfa_required", self.sign_in(self.browser()))
        self.assertEqual(self.audit("mfa_reset")[0]["details"]["via"], "host")
        missing = subprocess.run([sys.executable, "-m", "app.mfa", "reset", "nobody"], env={**os.environ}, capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(missing.returncode, 1)


class AuditTests(SecondFactorCase):
    def test_enrolment_use_and_failures_are_audited(self):
        secret, _ = self.enrol_totp(self.admin)
        self.enrol_passkey(self.admin, SoftwareAuthenticator())
        client = self.browser()
        challenge = self.sign_in(client)["challenge"]
        client.post("/api/auth/mfa/verify", json={"challenge": challenge, "method": "totp", "code": "000000"})
        client.post("/api/auth/mfa/verify", json={"challenge": challenge, "method": "totp", "code": mfa.totp_code(secret, mfa.totp_step() + 1)})
        self.admin.request("DELETE", "/api/me/security/totp", json={"password": TEST_PASSWORD})
        self.assertEqual(len(self.audit("mfa_totp_enrolled")), 1)
        self.assertEqual(len(self.audit("mfa_passkey_enrolled")), 1)
        self.assertEqual(len(self.audit("mfa_recovery_codes_generated")), 1)
        failed = self.audit("mfa_failed")
        self.assertEqual((failed[0]["details"]["method"], failed[0]["details"]["reason"], failed[0]["actor_id"]), ("totp", "wrong_code", 1))
        self.assertEqual(self.audit("mfa_used")[0]["details"]["method"], "totp")
        self.assertEqual(self.audit("mfa_factor_removed")[0]["details"]["factor"], "totp")
        with connect() as db:
            self.assertNotIn(secret.hex(), "".join(row[0] for row in db.execute("SELECT details_json FROM audit_log")))


class RateLimitTests(SecondFactorCase):
    def fail(self, username: str = "keeper", password: str = "wrong password 12345", client: TestClient | None = None):
        return (client or self.browser()).post("/api/auth/login", json={"username": username, "password": password})

    def test_repeated_failures_are_slowed_even_for_the_right_password(self):
        for _ in range(4):
            self.assertEqual(self.fail().status_code, 401)
        self.assertEqual(self.fail().status_code, 401)  # The fifth fails, and starts the wait.
        refused = self.fail()
        self.assertEqual(refused.status_code, 429)
        self.assertGreaterEqual(int(refused.headers["Retry-After"]), 1)
        right = self.browser().post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD})
        self.assertEqual(right.status_code, 429)  # A throttle that let the right password through would not slow guessing.
        throttled = self.audit("auth_throttled")
        self.assertEqual([(entry["resource_type"], entry["resource_id"]) for entry in throttled], [("account", "keeper")])

    def test_the_wait_grows_and_is_capped_so_nobody_is_locked_out_for_good(self):
        now = datetime.now(timezone.utc)
        waits = []
        for failures in range(1, 40):
            auth_limits.record_failure("keeper", f"10.0.0.{failures}")
            with connect() as db:
                row = db.execute("SELECT blocked_until FROM auth_failures WHERE scope='account' AND key='keeper'").fetchone()
            waits.append(0 if row["blocked_until"] is None else (datetime.fromisoformat(row["blocked_until"]) - now).total_seconds())
        self.assertEqual([round(wait) for wait in waits[:8]], [0, 0, 0, 0, 2, 4, 8, 16])
        self.assertTrue(all(second >= first - 1 for first, second in zip(waits, waits[1:])))
        self.assertLessEqual(max(waits), auth_limits.MAX_WAIT_SECONDS + 1)

        later = datetime.now(timezone.utc) + timedelta(seconds=auth_limits.MAX_WAIT_SECONDS + 5)
        with mock.patch.object(auth_limits, "_now", return_value=later):
            self.assertEqual(self.browser().post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 200)

    def test_a_completed_sign_in_clears_the_accounts_failures(self):
        for _ in range(3):
            self.fail()
        self.sign_in(self.browser())
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM auth_failures WHERE scope='account' AND key='keeper'").fetchone())

    def test_an_address_guessing_many_accounts_is_slowed_too(self):
        for index in range(20):  # The 20th starts the address's wait.
            self.assertEqual(self.fail(f"nobody{index}").status_code, 401)
        self.assertEqual(self.fail("nobody-new").status_code, 429)
        self.assertEqual(self.browser().post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 429)

    def test_failures_are_forgotten_after_a_quiet_spell(self):
        for _ in range(4):
            self.fail()
        later = datetime.now(timezone.utc) + auth_limits.FORGET_AFTER + timedelta(seconds=5)
        with mock.patch.object(auth_limits, "_now", return_value=later):
            self.assertEqual(self.fail().status_code, 401)
            with connect() as db:
                self.assertEqual(db.execute("SELECT failures FROM auth_failures WHERE scope='account'").fetchone()[0], 1)

    def test_second_factor_guesses_are_limited_too(self):
        self.enrol_totp(self.admin)
        for _ in range(5):
            client = self.browser()
            challenge = self.sign_in(client)["challenge"]
            wrong = client.post("/api/auth/mfa/verify", json={"challenge": challenge, "method": "totp", "code": "000000"})
            self.assertEqual(wrong.status_code, 401)
        # Guessing codes across fresh sign-ins does not get round it: the account now waits, password or not.
        self.assertEqual(self.browser().post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 429)

    def test_unknown_passkey_attempts_slow_the_address_without_blocking_the_account_bucket(self):
        authenticator = SoftwareAuthenticator()
        for _ in range(6):
            client = self.browser()
            started = client.post("/api/auth/passkey/options").json()
            client.post("/api/auth/passkey/login", json={"challenge": started["challenge"], "credential": authenticator.get(started["options"])})
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM auth_failures WHERE scope='account'").fetchone())
        self.assertEqual(self.browser().post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 200)


class MigrationTests(SecondFactorCase):
    def test_second_factor_tables_start_empty_with_the_policy_off(self):
        with connect() as db:
            self.assertEqual(mfa.policy(db), "off")
            for table in ("user_totp", "user_passkeys", "user_recovery_codes", "mfa_challenges", "auth_failures"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
