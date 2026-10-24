from __future__ import annotations

import base64
import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import web_interface
from app.application import create_app
from tests.test_support import TEST_PASSWORD


def png_size(data: bytes) -> tuple[int, int]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), "not a PNG"
    return struct.unpack(">II", data[16:24])


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg renders the app icons")
class InstallableAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        self.dist = root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text("<!doctype html><title>NVR</title>")
        shutil.copyfile(Path(__file__).resolve().parents[1] / "web" / "public" / "sw.js", self.dist / "sw.js")
        self.context = TestClient(create_app(start_workers=False))
        self.client = self.context.__enter__()
        self.assertEqual(self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 201)

    def tearDown(self):
        self.context.__exit__(None, None, None)
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.temporary.cleanup()

    def manifest(self) -> dict:
        response = TestClient(self.client.app).get("/manifest.webmanifest")  # no session: installing happens before sign-in
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/manifest+json")
        self.assertEqual(response.headers["cache-control"], "no-cache")
        return response.json()

    def test_the_manifest_uses_site_branding_and_offers_square_png_icons(self):
        manifest = self.manifest()
        self.assertEqual((manifest["name"], manifest["start_url"], manifest["scope"], manifest["display"]), ("Workplace NVR", "/", "/", "standalone"))
        self.assertEqual(manifest["theme_color"], "#17212b")
        sizes = {}
        for icon in manifest["icons"]:
            image = TestClient(self.client.app).get(icon["src"])
            self.assertEqual(image.status_code, 200, icon)
            self.assertEqual(image.headers["content-type"], "image/png")
            width, height = png_size(image.content)
            self.assertEqual(f"{width}x{height}", icon["sizes"])
            sizes.setdefault(icon["purpose"], set()).add(icon["sizes"])
        self.assertTrue({"192x192", "512x512"} <= sizes["any"])
        self.assertIn("512x512", sizes["maskable"])

    def test_changing_branding_changes_the_installed_name_and_icon(self):
        before = self.manifest()
        logo = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=300x200", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                              capture_output=True, check=True).stdout
        saved = self.client.put("/api/settings/branding", json={
            "app_name": "Harbour Security", "primary_color": "#0f3d2e", "accent_color": "#f6f064",
            "assets": {"icon": {"media_type": "image/png", "data_base64": base64.b64encode(logo).decode()}},
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        after = self.manifest()
        self.assertEqual((after["name"], after["short_name"], after["theme_color"]), ("Harbour Security", "Harbour Secu", "#0f3d2e"))
        self.assertNotEqual({icon["src"] for icon in after["icons"]}, {icon["src"] for icon in before["icons"]})
        icon = TestClient(self.client.app).get(next(icon["src"] for icon in after["icons"] if icon["sizes"] == "512x512"))
        self.assertEqual(png_size(icon.content), (512, 512))  # the uploaded 300x200 icon, made square

    def test_the_service_worker_is_versioned_by_the_built_page_and_never_cached_by_http(self):
        with patch.object(web_interface, "DIST_DIRECTORY", self.dist):
            worker = self.client.get("/sw.js")
            self.assertEqual(worker.status_code, 200)
            self.assertEqual(worker.headers["content-type"], "text/javascript; charset=utf-8")
            self.assertEqual(worker.headers["cache-control"], "no-cache")
            version = hashlib.sha256((self.dist / "index.html").read_bytes()).hexdigest()[:16]
            self.assertIn(f'"{version}"', worker.text)
            (self.dist / "index.html").write_text("<!doctype html><title>NVR next release</title>")
            self.assertNotIn(f'"{version}"', self.client.get("/sw.js").text)


if __name__ == "__main__":
    unittest.main()
