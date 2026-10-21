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
