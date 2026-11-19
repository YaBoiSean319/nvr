"""A local account's password can be changed from the account menu and set from the host (#366)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app import mfa
from app.store import connect
from tests.fixtures.software_authenticator import SoftwareAuthenticator
from tests.test_second_factors import OPERATOR_PASSWORD, SecondFactorCase
from tests.test_support import TEST_PASSWORD

NEW_PASSWORD = "a-new-password-98765"
ROOT = Path(__file__).resolve().parents[1]


class PasswordChangeTests(SecondFactorCase):
    def change(self, client=None, **extra):
        body = {"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD, **extra}
        return (client or self.admin).post("/api/me/security/password", json=body)

    def signs_in_with(self, password: str) -> int:
        return self.browser().post("/api/auth/login", json={"username": "keeper", "password": password}).status_code

    def test_the_current_password_changes_it_and_only_the_new_one_signs_in(self):
        self.assertEqual(self.change().status_code, 200)
        self.assertEqual(self.signs_in_with(TEST_PASSWORD), 401)
        self.assertEqual(self.signs_in_with(NEW_PASSWORD), 200)
        entry = self.audit("password_changed")[0]
        self.assertEqual((entry["actor_id"], entry["details"]["via"], entry["details"]["second_factor"]), (1, "account", None))
        self.assertNotIn(NEW_PASSWORD, entry["details_json"])

    def test_a_wrong_current_password_changes_nothing_and_is_audited(self):
        refused = self.change(current_password="not the password 12345")
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(self.signs_in_with(TEST_PASSWORD), 200)
        self.assertEqual(self.audit("mfa_failed")[-1]["details"]["method"], "password_confirmation")
        self.assertEqual(self.audit("password_changed"), [])

    def test_the_first_run_password_rules_apply(self):
        for weak in ("short1", "no digits in this password", "12345678901234567890", TEST_PASSWORD):
            refused = self.change(new_password=weak)
            self.assertEqual(refused.status_code, 422, weak)
        self.assertEqual(self.signs_in_with(TEST_PASSWORD), 200)

    def test_other_sessions_and_device_sign_ins_end_and_this_one_stays(self):
        elsewhere = self.browser()
        self.sign_in(elsewhere)
        with connect() as db:
            columns = [row["name"] for row in db.execute("PRAGMA table_info(device_sessions)") if row["notnull"] and row["dflt_value"] is None and not row["pk"]]
            db.execute(f"INSERT INTO device_sessions({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                       tuple(1 if name == "user_id" else f"x-{name}" for name in columns))
        self.assertEqual(self.change().status_code, 200)
        self.assertEqual(elsewhere.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.admin.get("/api/auth/me").status_code, 200)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM device_sessions").fetchone()[0], 0)

    def test_an_account_with_an_authenticator_must_give_its_code_too(self):
        secret, codes = self.enrol_totp(self.admin)
        self.assertEqual(self.change().status_code, 403)
        self.assertEqual(self.change(method="totp", code="000000").status_code, 403)
        self.assertEqual(self.audit("mfa_failed")[-1]["details"]["method"], "password_change_totp")
        self.assertEqual(self.signs_in_with(NEW_PASSWORD), 401)
        changed = self.change(method="totp", code=mfa.totp_code(secret, mfa.totp_step() + 1))
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(self.audit("password_changed")[0]["details"]["second_factor"], "totp")
        self.assertEqual(self.signs_in_with(NEW_PASSWORD), 200)

    def test_a_recovery_code_answers_for_the_second_factor_once(self):
        _, codes = self.enrol_totp(self.admin)
        self.assertEqual(self.change(method="recovery", code=codes[0]).status_code, 200)
        again = self.admin.post("/api/me/security/password", json={"current_password": NEW_PASSWORD, "new_password": "another-password-4321", "method": "recovery", "code": codes[0]})
        self.assertEqual(again.status_code, 403)

    def test_a_passkey_answers_for_the_second_factor(self):
        authenticator = SoftwareAuthenticator()
        self.enrol_passkey(self.admin, authenticator)
        self.assertEqual(self.admin.post("/api/me/security/password/passkey-options", json={"password": "not the password 12345"}).status_code, 403)
        begun = self.admin.post("/api/me/security/password/passkey-options", json={"password": TEST_PASSWORD})
        self.assertEqual(begun.status_code, 200, begun.text)
        stranger = SoftwareAuthenticator()
        forged = self.change(method="passkey", challenge=begun.json()["challenge"], credential=stranger.get(begun.json()["options"]))
        self.assertEqual(forged.status_code, 403, forged.text)
        begun = self.admin.post("/api/me/security/password/passkey-options", json={"password": TEST_PASSWORD}).json()
        changed = self.change(method="passkey", challenge=begun["challenge"], credential=authenticator.get(begun["options"]))
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(self.signs_in_with(TEST_PASSWORD), 401)

    def test_one_persons_passkey_challenge_does_not_change_anothers_password(self):
        authenticator = SoftwareAuthenticator()
        self.enrol_passkey(self.admin, authenticator)
        self.add_operator()
        operator = self.browser()
        self.sign_in(operator, "operator", OPERATOR_PASSWORD)
        self.enrol_totp(operator)
        begun = self.admin.post("/api/me/security/password/passkey-options", json={"password": TEST_PASSWORD}).json()
        refused = operator.post("/api/me/security/password", json={"current_password": OPERATOR_PASSWORD, "new_password": NEW_PASSWORD, "method": "passkey",
                                                                   "challenge": begun["challenge"], "credential": authenticator.get(begun["options"])})
        self.assertEqual(refused.status_code, 403, refused.text)

    def test_an_api_token_cannot_change_the_password(self):
        created = self.admin.post("/api/admin/api-tokens", json={"name": "ci", "scopes": ["admin"]})
        headers = {"Authorization": f"Bearer {created.json()['secret']}"}
        self.assertEqual(self.change(self.browser()).status_code, 401)
        refused = self.browser().post("/api/me/security/password", headers=headers, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})
        self.assertEqual(refused.status_code, 403)


class HostPasswordTests(SecondFactorCase):
    def host(self, username: str, typed: str):
        return subprocess.run([sys.executable, "-m", "app.mfa", "password", username], input=typed, env={**os.environ}, capture_output=True, text=True, cwd=ROOT)

    def test_the_host_sets_a_password_when_nobody_can_sign_in(self):
        secret, _ = self.enrol_totp(self.admin)
        run = self.host("keeper", NEW_PASSWORD + "\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn(NEW_PASSWORD, run.stdout + run.stderr)
        self.assertEqual(self.admin.get("/api/auth/me").status_code, 401)  # Every session ended, this one included
        client = self.browser()
        self.assertEqual(client.post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 401)
        self.assertIn("challenge", self.sign_in(client, password=NEW_PASSWORD))  # The second factor is still asked for
        entry = self.audit("password_changed")[0]
        self.assertEqual((entry["actor_id"], entry["details"]["via"]), (None, "host"))
        self.assertNotIn(NEW_PASSWORD, entry["details_json"])

    def test_the_host_refuses_a_weak_password_an_unknown_name_and_an_entra_account(self):
        self.assertEqual(self.host("keeper", "short1\n").returncode, 1)
        self.assertEqual(self.host("nobody", NEW_PASSWORD + "\n").returncode, 1)
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role,provider_issuer,provider_subject) VALUES('entra@example.test','x','operator','https://login.example.test','s')")
        self.assertEqual(self.host("entra@example.test", NEW_PASSWORD + "\n").returncode, 1)
        self.assertEqual(self.sign_in(self.browser()).get("username", "keeper"), "keeper")
        self.assertEqual(self.audit("password_changed"), [])
