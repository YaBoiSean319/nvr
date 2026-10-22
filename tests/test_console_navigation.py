import re
import unittest

from fastapi.testclient import TestClient

from app.main import app


class ConsoleNavigationTests(unittest.TestCase):
    def test_console_shell_exposes_keyboard_accessible_screen_navigation(self):
        response = TestClient(app).get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="console-nav"', response.text)
        self.assertIn('data-screen="live"', response.text)
        self.assertIn('data-screen="footage"', response.text)
        self.assertIn('data-screen="camera-settings"', response.text)
        self.assertIn('data-screen="app-settings"', response.text)
        self.assertIn('id="console-sidebar"', response.text)
        self.assertIn('id="sidebar-search"', response.text)
        self.assertIn('<script src="/camera-inventory.js" defer></script>', response.text)
        self.assertIn('<script src="/camera-editor.js" defer></script>', response.text)
        self.assertIn('<script src="/camera-discovery.js" defer></script>', response.text)
        self.assertLess(response.text.index('src="/camera-editor.js"'), response.text.index('src="/app.js"'))
        self.assertLess(response.text.index('src="/camera-discovery.js"'), response.text.index('src="/app.js"'))
        client = TestClient(app)
        script_paths = re.findall(r'<script\s+src="(/[^\"]+\.js)"', response.text)
        stylesheet_paths = re.findall(r'<link\s+[^>]*href="(/[^\"]+\.css)"', response.text)
        self.assertTrue(script_paths)
        for script_path in script_paths:
            with self.subTest(script=script_path):
                script = client.get(script_path)
                self.assertEqual(script.status_code, 200)
                self.assertIn("javascript", script.headers.get("content-type", ""))
        for stylesheet_path in stylesheet_paths:
            with self.subTest(stylesheet=stylesheet_path):
                stylesheet = client.get(stylesheet_path)
                self.assertEqual(stylesheet.status_code, 200)
                self.assertIn("text/css", stylesheet.headers.get("content-type", ""))
        inventory_script = client.get("/camera-inventory.js")
        self.assertIn("filterAndSortCameras", inventory_script.text)
        self.assertEqual(client.get("/unlisted.js").status_code, 404)
        self.assertEqual(client.get("/unlisted.css").status_code, 404)

    def test_live_grid_offers_the_four_approved_capacities(self):
        response = TestClient(app).get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="live-grid-size"', response.text)
        for capacity in ("1", "4", "9", "16"):
            self.assertIn(f'<option value="{capacity}"', response.text)
        self.assertIn('aria-live="polite"', response.text)

    def test_footage_search_exposes_stale_state_and_shared_timeline_controls(self):
        response = TestClient(app).get("/")

        self.assertEqual(response.status_code, 200)
        for control in (
            'id="playback-camera-picker"', 'id="playback-start"', 'id="playback-end"',
            'id="playback-stale"', 'id="playback-ruler"', 'id="playback-zoom-in"',
            'id="playback-zoom-out"', 'id="playback-fit-range"',
        ):
            self.assertIn(control, response.text)
        self.assertIn("New searches start with the past hour", response.text)


if __name__ == "__main__":
    unittest.main()
