"""A person's access can be taken away, and losing the Entra role ends it (#364).

Four things kept working after a role was removed in Entra: the open browser session, a device sign-in (which hands out sessions and extended
itself for ever), any API token the person made, and alarm notifications. Each test repeats those four and shows they have ended."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import device_sessions
from app.store import connect
from tests.test_access_grants import GrantCase
from tests.test_bootstrap import OIDC_TEST_TENANT
from tests.test_second_factors import OPERATOR_PASSWORD
from tests.test_support import TEST_OIDC_CLIENT_SECRET, TEST_OIDC_SESSION_SECRET

OIDC = {"NVR_PUBLIC_URL": "https://nvr.example.test", "NVR_OIDC_TENANT_ID": OIDC_TEST_TENANT, "NVR_OIDC_CLIENT_ID": "client-id",
        "NVR_OIDC_CLIENT_SECRET": TEST_OIDC_CLIENT_SECRET, "NVR_OIDC_SESSION_SECRET": TEST_OIDC_SESSION_SECRET,
        "NVR_OIDC_OPERATOR_ROLES": "NVR.Operator", "NVR_OIDC_ADMIN_ROLES": "NVR.Admin"}


class RemovalCase(GrantCase):
    def setUp(self):
        super().setUp()
        os.environ.update(OIDC)
        for key in OIDC:
            self.addCleanup(os.environ.pop, key, None)

    def entra(self, roles: list[str], subject: str = "object-1") -> tuple[TestClient, int]:
        """A sign-in through the stand-in for Entra, with the roles it says the person holds: (their browser, the answer's status)."""
        claims = {"sub": subject, "tid": OIDC_TEST_TENANT, "iss": f"https://login.microsoftonline.com/{OIDC_TEST_TENANT}/v2.0",
                  "preferred_username": f"{subject}@example.test", "roles": roles}
        fake = type("FakeOIDC", (), {"authorize_access_token": AsyncMock(return_value={"userinfo": claims})})()
        client = TestClient(self.app, base_url="https://testserver.local")
        with patch("app.api.identity._oidc_client", return_value=fake):
            return client, client.get("/api/auth/oidc/callback?code=fake&state=fake", follow_redirects=False).status_code

    def everything(self, client: TestClient, user_id: int) -> dict[str, object]:
        """Give a signed-in person the four things: returns the device secret and the API token (their browser session is ``client``)."""
        device = client.post("/api/native/device-sessions", json={"device_label": "Pixel"})
        self.assertEqual(device.status_code, 201, device.text)
        with connect() as db:
            db.execute("INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh,auth) VALUES('p1',?,'https://push.example.test/1','k','a')", (user_id,))
            role = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()[0]
        token = None
        if role == "administrator":
            created = client.post("/api/admin/api-tokens", json={"name": "theirs", "scopes": ["admin"]})
            self.assertEqual(created.status_code, 201, created.text)
            token = created.json()["secret"]
        return {"device": device.json()["token"], "token": token}

    def still_works(self, client: TestClient, held: dict[str, object], user_id: int) -> dict[str, bool]:
        with connect() as db:
            pushes = db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE user_id=?", (user_id,)).fetchone()[0]
        base = str(client.base_url)
        works = {"session": client.get("/api/auth/me").status_code == 200,
                 "device": TestClient(self.app, base_url=base).post("/api/native/device-sessions/resume", json={"token": held["device"]}).status_code == 200,
                 "push": pushes > 0}
        if held["token"]:
            works["token"] = TestClient(self.app, base_url=base).get("/api/cameras", headers={"Authorization": f"Bearer {held['token']}"}).status_code == 200
        return works

    def user_id(self, subject: str = "object-1") -> int:
        with connect() as db:
            return int(db.execute("SELECT id FROM users WHERE provider_subject=?", (subject,)).fetchone()[0])

    def audit(self, action: str) -> list[dict]:
        import json
        with connect() as db:
            return [{**dict(row), "details": json.loads(row["details_json"])} for row in db.execute("SELECT * FROM audit_log WHERE action=? ORDER BY id", (action,))]


class EntraRoleRemovedTests(RemovalCase):
    def test_a_refused_entra_sign_in_ends_what_the_person_had(self):
        client, status = self.entra(["NVR.Admin"])
        self.assertEqual(status, 303)
        user_id = self.user_id()
        held = self.everything(client, user_id)
        self.assertEqual(self.still_works(client, {**held, "device": self.everything_device(client)}, user_id), {"session": True, "device": True, "push": True, "token": True})

        _, status = self.entra([])  # The role was removed in Entra, and they try to sign in
        self.assertEqual(status, 403)
        self.assertEqual(self.still_works(client, held, user_id), {"session": False, "device": False, "push": False, "token": False})
        entry = self.audit("user_access_ended")
        self.assertEqual(len(entry), 1)
        self.assertEqual((entry[0]["details"]["reason"], entry[0]["details"]["api_tokens"], entry[0]["details"]["push_subscriptions"]), ("entra_role_removed", 1, 1))
        listed = {row["id"]: row for row in self.admin.get("/api/admin/users").json()["users"]}[user_id]
        self.assertTrue(listed["entra_role_removed"])

        self.entra([])  # Refused again: there is nothing left to end, and nothing more is written
        self.assertEqual(len(self.audit("user_access_ended")), 1)

    def everything_device(self, client: TestClient) -> str:
        return client.post("/api/native/device-sessions", json={"device_label": "Spare"}).json()["token"]

    def test_someone_the_nvr_never_knew_changes_nothing(self):
        _, status = self.entra([], subject="stranger")
        self.assertEqual(status, 403)
        self.assertEqual(self.audit("user_access_ended"), [])
        self.assertEqual(self.admin.get("/api/auth/me").status_code, 200)

    def test_a_role_given_back_signs_them_in_again_but_restores_nothing(self):
        client, _ = self.entra(["NVR.Admin"])
        held = self.everything(client, self.user_id())
        self.entra([])
        again, status = self.entra(["NVR.Operator"])
        self.assertEqual(status, 303)
        self.assertEqual(again.get("/api/auth/me").json()["role"], "operator")
        self.assertEqual(self.still_works(client, held, self.user_id()), {"session": False, "device": False, "push": False, "token": False})


class EntraDeviceSignInTests(RemovalCase):
    def setUp(self):
        super().setUp()
        self.start = datetime.now(timezone.utc)
        self.clock = [self.start]
        patcher = patch("app.device_sessions._now", lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def day(self, number: float) -> None:
        self.clock[0] = self.start + timedelta(days=number)

    def resume(self, secret: str, base: str) -> tuple[int, str | None, TestClient]:
        phone = TestClient(self.app, base_url=base)
        answer = phone.post("/api/native/device-sessions/resume", json={"token": secret})
        return answer.status_code, answer.json().get("token") if answer.status_code == 200 else None, phone

    def test_an_entra_device_sign_in_needs_entra_again_after_the_set_days_however_often_it_is_used(self):
        client, _ = self.entra(["NVR.Operator"])
        secret = client.post("/api/native/device-sessions", json={"device_label": "Pixel"}).json()["token"]
        base = str(client.base_url)
        for day in (10, 20, 29):
            self.day(day)
            status, secret, phone = self.resume(secret, base)
            self.assertEqual(status, 200, day)
        # A new device sign-in made from a session the old one produced counts from the same Entra sign-in
        chained = phone.post("/api/native/device-sessions", json={"device_label": "Second"}).json()
        self.assertLessEqual(chained["expires_at"], device_sessions.stamp(self.start + device_sessions.ENTRA_PROOF_LIFETIME + timedelta(seconds=5)))
        self.day(31)
        self.assertEqual(self.resume(secret, base)[0], 401)
        self.assertEqual(self.resume(chained["token"], base)[0], 401)
        # Signing in with Entra again starts a new count
        client, _ = self.entra(["NVR.Operator"])
        fresh = client.post("/api/native/device-sessions", json={"device_label": "Pixel"}).json()["token"]
        self.day(50)
        self.assertEqual(self.resume(fresh, base)[0], 200)

    def test_a_local_accounts_device_sign_in_still_lasts_while_it_is_used(self):
        secret = self.limited.post("/api/native/device-sessions", json={"device_label": "Pixel"}).json()["token"]
        for day in (25, 50, 75):
            self.day(day)
            status, secret, _ = self.resume(secret, "http://testserver")
            self.assertEqual(status, 200, day)


class DisableTests(RemovalCase):
    def disable(self, user_id: int, disabled: bool = True, client: TestClient | None = None):
        return (client or self.admin).put(f"/api/admin/users/{user_id}/disabled", json={"disabled": disabled})

    def sign_in_status(self, name: str = "limited") -> int:
        return TestClient(self.app).post("/api/auth/login", json={"username": name, "password": OPERATOR_PASSWORD}).status_code

    def test_disabling_a_person_ends_the_four_things_and_is_audited(self):
        with connect() as db:
            db.execute("UPDATE users SET role='administrator' WHERE id=?", (self.limited_id,))
        held = self.everything(self.limited, self.limited_id)
        self.assertEqual(self.disable(self.limited_id).status_code, 200)
        self.assertEqual(self.still_works(self.limited, held, self.limited_id), {"session": False, "device": False, "push": False, "token": False})
        entry = self.audit("user_disabled")[0]
        self.assertEqual((entry["actor_id"], entry["details"]["username"], entry["details"]["sessions"], entry["details"]["api_tokens"]), (1, "limited", 1, 1))
        listed = {row["id"]: row for row in self.admin.get("/api/admin/users").json()["users"]}
        self.assertTrue(listed[self.limited_id]["disabled"])
        self.assertFalse(listed[1]["disabled"])

    def test_a_disabled_account_cannot_sign_in_resume_a_device_or_use_a_token_even_one_left_behind(self):
        with connect() as db:
            db.execute("UPDATE users SET role='administrator' WHERE id=?", (self.limited_id,))
        held = self.everything(self.limited, self.limited_id)
        with connect() as db:  # As if something were missed: only the mark is set, and nothing was ended
            db.execute("UPDATE users SET disabled_at='2026-01-01T00:00:00.000Z' WHERE id=?", (self.limited_id,))
        works = self.still_works(self.limited, held, self.limited_id)
        self.assertEqual((works["session"], works["device"], works["token"]), (False, False, False))
        self.assertEqual(self.sign_in_status(), 401)

    def test_enabling_lets_them_sign_in_again_and_brings_nothing_back(self):
        held = self.everything(self.limited, self.limited_id)
        self.disable(self.limited_id)
        self.assertEqual(self.sign_in_status(), 401)
        self.assertEqual(self.disable(self.limited_id, False).status_code, 200)
        self.assertEqual(self.sign_in_status(), 200)
        self.assertEqual(self.still_works(self.limited, held, self.limited_id), {"session": False, "device": False, "push": False})
        self.assertEqual(len(self.audit("user_enabled")), 1)

    def test_nobody_disables_themselves_so_an_administrator_always_remains(self):
        self.assertEqual(self.disable(1).status_code, 409)
        self.assertEqual(self.admin.get("/api/auth/me").status_code, 200)
        created = self.admin.post("/api/admin/api-tokens", json={"name": "ci", "scopes": ["admin"]}).json()["secret"]
        by_token = TestClient(self.app).put("/api/admin/users/1/disabled", json={"disabled": True}, headers={"Authorization": f"Bearer {created}"})
        self.assertEqual(by_token.status_code, 409)

    def test_only_administrators_disable_and_an_unknown_person_is_not_found(self):
        self.assertEqual(self.disable(1, client=self.limited).status_code, 403)
        self.assertEqual(self.disable(999).status_code, 404)

    def test_a_disabled_entra_account_is_refused_at_entra_sign_in(self):
        client, _ = self.entra(["NVR.Operator"])
        self.disable(self.user_id())
        _, status = self.entra(["NVR.Operator"])
        self.assertEqual(status, 403)
        self.assertEqual(client.get("/api/auth/me").status_code, 401)
