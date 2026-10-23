from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import web_next
from app.application import create_app
from tests.test_support import TEST_PASSWORD


class WebInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        self.dist = root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text("<!doctype html><title>NVR</title>")
        (self.dist / "assets" / "index-abc123.js").write_text("console.log(1)")
        (root / "secret.txt").write_text("not public")
        self.context = TestClient(create_app(start_workers=False))
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.temporary.cleanup()

    def test_client_routes_get_the_page_with_security_headers(self):
        with patch.object(web_next, "DIST_DIRECTORY", self.dist):
            self.assertEqual(self.client.get("/next", follow_redirects=False).headers["location"], "/next/")
            for path in ("/next/", "/next/live", "/next/settings/branding"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, path)
                self.assertIn("<title>NVR</title>", response.text)
                self.assertEqual(response.headers["cache-control"], "no-cache")
                self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
                self.assertIn("script-src 'self'", response.headers["content-security-policy"])
            asset = self.client.get("/next/assets/index-abc123.js")
            self.assertEqual(asset.status_code, 200)
            self.assertIn("immutable", asset.headers["cache-control"])
            self.assertEqual(self.client.get("/next/assets/missing.js").status_code, 404)
            self.assertEqual(self.client.get("/next/assets/..%2F..%2Fsecret.txt").status_code, 404)

    def test_missing_build_is_reported_honestly(self):
        with patch.object(web_next, "DIST_DIRECTORY", self.dist / "absent"):
            response = self.client.get("/next/live")
        self.assertEqual(response.status_code, 503)
        self.assertIn("not been built", response.json()["detail"])

    def test_theme_preference_is_personal(self):
        self.assertEqual(self.client.get("/api/me/preferences").status_code, 401)
        self.assertEqual(self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 201)
        self.assertEqual(self.client.get("/api/me/preferences").json(), {"theme": "system"})
        self.assertEqual(self.client.put("/api/me/preferences", json={"theme": "dark"}).json(), {"theme": "dark"})
        self.assertEqual(self.client.put("/api/me/preferences", json={"theme": "neon"}).status_code, 422)
        self.assertEqual(self.client.get("/api/me/preferences").json(), {"theme": "dark"})


if __name__ == "__main__":
    unittest.main()
