import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NVR_DB_PATH"] = str(Path(self.tmp.name) / "state.sqlite3")
        os.environ["NVR_COOKIE_SECURE"] = "false"
        from app.main import app
        self.client_ctx = TestClient(app)
        self.client = self.client_ctx.__enter__()

    def tearDown(self):
        self.client_ctx.__exit__(None, None, None)
        self.tmp.cleanup()
        os.environ.pop("NVR_DB_PATH", None)
        os.environ.pop("NVR_COOKIE_SECURE", None)

    def test_bootstrap_closes_setup_and_authenticates_admin(self):
        self.assertEqual(self.client.get("/api/setup/status").json(), {"setup_required": True})
        response = self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["role"], "administrator")
        self.assertEqual(self.client.get("/api/setup/status").json(), {"setup_required": False})
        self.assertEqual(self.client.get("/api/system/status").status_code, 200)
        self.assertEqual(self.client.post("/api/setup/admin", json={"username":"second-admin", "password":"AnotherStrongPass2026"}).status_code, 409)

    def test_api_denies_status_and_invalid_credentials(self):
        self.assertEqual(self.client.get("/api/system/status").status_code, 401)
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.client.post("/api/auth/logout")
        bad = self.client.post("/api/auth/login", json={"username":"local-admin", "password":"wrong"})
        self.assertEqual(bad.status_code, 401)

    def test_restart_preserves_account_and_health(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.client_ctx.__exit__(None, None, None)
        from app.main import app
        with TestClient(app) as restarted:
            self.assertEqual(restarted.get("/healthz").status_code, 200)
            self.assertEqual(restarted.get("/api/setup/status").json(), {"setup_required": False})
            self.assertEqual(restarted.get("/api/system/status").status_code, 401)
            self.assertEqual(restarted.post("/api/auth/login", json={"username":"local-admin", "password":"StrongPassphrase2026"}).status_code, 200)

    def test_password_policy_and_username_validation(self):
        self.assertEqual(self.client.post("/api/setup/admin", json={"username":"a", "password":"short"}).status_code, 422)
        self.assertEqual(self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"longpasswordonly"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
