from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.application import create_app
from app.principal import COOKIE_NAME
from app.security import hash_password
from app.store import connect
from tests.test_support import TEST_PASSWORD


class APITokenTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH")}
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        self.context = TestClient(create_app(start_workers=False))
        self.client = self.context.__enter__()
        created = self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD})
        self.assertEqual(created.status_code, 201, created.text)
        self.admin_cookie = self.client.cookies.get(COOKIE_NAME)

    def tearDown(self):
        self.context.__exit__(None, None, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def _create(self, *scopes: str, **extra) -> tuple[dict, str]:
        response = self.client.post("/api/admin/api-tokens", json={"name": "Integration", "scopes": list(scopes), **extra})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["token"], response.json()["secret"]

    def _bearer(self, method: str, path: str, secret: str, **kwargs):
        # A bare client: no session cookie, so only the token authenticates.
        with TestClient(self.client.app) as anonymous:
            return anonymous.request(method, path, headers={"Authorization": f"Bearer {secret}"}, **kwargs)

    def _audit(self, action: str) -> list[dict]:
        with connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM audit_log WHERE action=? ORDER BY id", (action,))]

    def test_secret_is_shown_once_and_stored_only_as_a_keyed_digest(self):
        token, secret = self._create("read")
        self.assertTrue(secret.startswith(f"nvrt_{token['id']}_"))
        self.assertEqual(token["scopes"], ["read"])
        self.assertEqual(token["state"], "active")
        self.assertEqual(token["created_by"], "keeper")

        listed = self.client.get("/api/admin/api-tokens").json()
        self.assertEqual(listed["scopes"], ["read", "media", "export", "admin"])
        self.assertNotIn(secret, str(listed))
        self.assertNotIn("secret", listed["tokens"][0])
        self.assertNotIn("secret_digest", listed["tokens"][0])
        with connect() as db:
            stored = [str(value) for row in db.execute("SELECT * FROM api_tokens") for value in tuple(row)]
            audit = [str(value) for row in db.execute("SELECT * FROM audit_log") for value in tuple(row)]
        self.assertFalse(any(secret.split("_", 2)[-1] in value for value in stored + audit))
        self.assertEqual(self._audit("api_token_created")[0]["resource_id"], token["id"])

    def test_secrets_containing_separators_authenticate(self):
        with patch("app.api_tokens.secrets.token_urlsafe", return_value="a_b_c-d_e"):
            _, secret = self._create("read")
        self.assertEqual(self._bearer("GET", "/api/cameras", secret).status_code, 200)

    def test_only_administrator_sessions_manage_tokens(self):
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", ("watcher", hash_password(TEST_PASSWORD), "operator"))
        with TestClient(self.client.app) as other:
            self.assertEqual(other.post("/api/admin/api-tokens", json={"name": "x", "scopes": ["read"]}).status_code, 401)
            self.assertEqual(other.post("/api/auth/login", json={"username": "watcher", "password": TEST_PASSWORD}).status_code, 200)
            self.assertEqual(other.post("/api/admin/api-tokens", json={"name": "x", "scopes": ["read"]}).status_code, 403)
            self.assertEqual(other.get("/api/admin/api-tokens").status_code, 403)
        _, admin_token = self._create("admin")
        listing = self._bearer("GET", "/api/admin/api-tokens", admin_token)
        self.assertEqual(listing.status_code, 403, listing.text)
        self.assertEqual(self._bearer("POST", "/api/admin/api-tokens", admin_token, json={"name": "y", "scopes": ["read"]}).status_code, 403)

    def test_validation_rejects_bad_requests(self):
        for body in (
            {"name": " ", "scopes": ["read"]},
            {"name": "x", "scopes": []},
            {"name": "x", "scopes": ["everything"]},
            {"name": "x", "scopes": ["read"], "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/admin/api-tokens", json=body).status_code, 422)

    def test_each_scope_reaches_only_its_operations(self):
        _, read = self._create("read")
        _, media = self._create("media")
        _, export = self._create("export")
        _, admin = self._create("admin")

        self.assertEqual(self._bearer("GET", "/api/cameras", read).status_code, 200)
        denied = self._bearer("POST", "/api/camera-groups", read, json={"name": "Dock"})
        self.assertEqual(denied.status_code, 403)
        self.assertIn("'admin' scope", denied.json()["detail"])
        self.assertEqual(self._bearer("GET", "/api/system/status", read).status_code, 403)
        self.assertEqual(self._bearer("GET", "/api/exports", read).status_code, 403)
        self.assertEqual(self._bearer("GET", "/api/cameras/missing/segments/1/media", read).status_code, 403)

        self.assertEqual(self._bearer("GET", "/api/cameras/missing/segments/1/media", media).status_code, 404)
        self.assertEqual(self._bearer("GET", "/api/cameras", media).status_code, 403)

        self.assertEqual(self._bearer("GET", "/api/exports", export).status_code, 200)
        self.assertEqual(self._bearer("GET", "/api/exports/missing/download", export).status_code, 404)
        self.assertEqual(self._bearer("GET", "/api/cameras", export).status_code, 403)

        self.assertEqual(self._bearer("GET", "/api/cameras", admin).status_code, 200)
        self.assertEqual(self._bearer("GET", "/api/exports", admin).status_code, 200)
        self.assertEqual(self._bearer("GET", "/api/system/status", admin).status_code, 200)
        self.assertEqual(self._bearer("POST", "/api/camera-groups", admin, json={"name": "Dock"}).status_code, 201)
        # A token acts for the administrator who created it.
        self.assertEqual(self._bearer("GET", "/api/auth/me", read).json(), {"username": "keeper", "role": "administrator"})

    def test_rejected_tokens_return_401_with_bearer_challenge(self):
        token, secret = self._create("read")
        tampered = secret[:-2] + ("AA" if not secret.endswith("AA") else "BB")
        for presented in ("garbage", f"nvrt_{token['id']}_", tampered, "nvrt_unknown_secret"):
            with self.subTest(presented=presented):
                response = self._bearer("GET", "/api/cameras", presented)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
        with TestClient(self.client.app) as anonymous:
            self.assertEqual(anonymous.get("/api/cameras", headers={"Authorization": f"Basic {secret}"}).status_code, 401)
            self.assertEqual(anonymous.get(f"/api/cameras?access_token={secret}").status_code, 401)
            self.assertEqual(anonymous.get(f"/api/cameras?token={secret}").status_code, 401)
            anonymous.cookies.set(COOKIE_NAME, secret)
            self.assertEqual(anonymous.get("/api/cameras").status_code, 401)

    def test_revoked_expired_and_orphaned_tokens_stop_working(self):
        revoked, revoked_secret = self._create("read")
        self.assertEqual(self._bearer("GET", "/api/cameras", revoked_secret).status_code, 200)
        response = self.client.delete(f"/api/admin/api-tokens/{revoked['id']}")
        self.assertEqual(response.json()["token"]["state"], "revoked")
        rejected = self._bearer("GET", "/api/cameras", revoked_secret)
        self.assertEqual((rejected.status_code, rejected.json()["detail"]), (401, "API token has been revoked"))
        self.assertEqual(len(self._audit("api_token_revoked")), 1)
        self.assertEqual(self.client.delete("/api/admin/api-tokens/unknown").status_code, 404)

        expiring, expiring_secret = self._create("read", expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        self.assertEqual(self._bearer("GET", "/api/cameras", expiring_secret).status_code, 200)
        with connect() as db:
            db.execute("UPDATE api_tokens SET expires_at=? WHERE id=?", ("2020-01-01T00:00:00.000Z", expiring["id"]))
        self.assertEqual(self._bearer("GET", "/api/cameras", expiring_secret).json()["detail"], "API token has expired")
        listed = {token["id"]: token["state"] for token in self.client.get("/api/admin/api-tokens").json()["tokens"]}
        self.assertEqual(listed[expiring["id"]], "expired")

        _, orphan_secret = self._create("read")
        with connect() as db:
            db.execute("UPDATE users SET role='operator' WHERE username='keeper'")
        self.assertEqual(self._bearer("GET", "/api/cameras", orphan_secret).status_code, 401)

    def test_use_is_recorded_with_bounded_writes(self):
        token, secret = self._create("read")
        for _ in range(3):
            self.assertEqual(self._bearer("GET", "/api/cameras", secret).status_code, 200)
        used = self._audit("api_token_used")
        self.assertEqual(len(used), 1)
        self.assertEqual(used[0]["resource_id"], token["id"])
        listed = self.client.get("/api/admin/api-tokens").json()["tokens"][0]
        self.assertIsNotNone(listed["last_used_at"])

    def test_openapi_documents_scope_per_operation(self):
        schema = self.client.get("/openapi.json").json()
        self.assertEqual(schema["components"]["securitySchemes"]["apiToken"]["scheme"], "bearer")
        self.assertEqual(schema["paths"]["/api/cameras"]["get"]["x-required-scope"], "read")
        self.assertEqual(schema["paths"]["/api/cameras"]["post"]["x-required-scope"], "admin")
        self.assertEqual(schema["paths"]["/api/exports"]["post"]["x-required-scope"], "export")
        self.assertEqual(schema["paths"]["/api/cameras/{camera_id}/segments/{segment_id}/media"]["get"]["x-required-scope"], "media")
        self.assertNotIn("x-required-scope", schema["paths"]["/api/setup/status"]["get"])
        self.assertIn("`read` scope", schema["paths"]["/api/cameras"]["get"]["description"])


if __name__ == "__main__":
    unittest.main()
