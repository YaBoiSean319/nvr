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


if __name__ == "__main__":
    unittest.main()
