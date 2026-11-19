"""With the credential key gone, the NVR says so in plain words instead of failing, and the pages that list stored addresses still open (#383)."""
from __future__ import annotations

import unittest

from cryptography.fernet import Fernet

from app import credentials
from app.credentials import key_path
from app.store import connect
from app.webhooks import url_origin
from tests.test_access_grants import GrantCase


class MissingKeyRequestTests(GrantCase):
    def setUp(self):
        super().setUp()
        made = self.admin.post("/api/webhooks", json={"name": "Receiver", "url": "http://192.0.2.10:8090/events/private-token", "events": ["recording.failure"], "allow_private_receiver": True})
        self.assertEqual(made.status_code, 201, made.text)
        self.original = key_path().read_bytes()

    def test_a_request_that_needs_the_key_is_answered_with_what_is_wrong(self):
        key_path().unlink()
        with self.assertLogs("nvr.credentials", level="ERROR"):
            answer = self.admin.post("/api/webhooks", json={"name": "Second", "url": "http://192.0.2.11:8090/events", "events": ["recording.failure"], "allow_private_receiver": True})
        self.assertEqual(answer.status_code, 503, answer.text)
        self.assertIn("credential key file is missing", answer.json()["detail"])
        self.assertFalse(key_path().exists())
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM webhook_endpoints").fetchone()[0], 1)
        key_path().write_bytes(self.original)
        self.assertEqual(self.admin.get("/api/webhooks").json()["webhooks"][0]["url_origin"], "http://192.0.2.10:8090")

    def test_the_webhook_and_heartbeat_pages_open_when_a_value_was_saved_under_another_key(self):
        configured = self.admin.put("/api/heartbeat", json={"target_url": "http://192.0.2.12:8090/beat/private-token", "interval_seconds": 900, "allow_private_receiver": True})
        self.assertEqual(configured.status_code, 200, configured.text)
        key_path().unlink()
        # A different key file is in place (the wrong one was restored): nothing saved before can be read with it.
        key_path().write_bytes(Fernet.generate_key())
        webhooks = self.admin.get("/api/webhooks")
        self.assertEqual(webhooks.status_code, 200, webhooks.text)
        self.assertIn("cannot be read", webhooks.json()["webhooks"][0]["url_origin"])
        self.assertNotIn("private-token", webhooks.text)
        heartbeat = self.admin.get("/api/heartbeat")
        self.assertEqual(heartbeat.status_code, 200, heartbeat.text)
        self.assertIn("cannot be read", heartbeat.text)
        self.assertNotIn("private-token", heartbeat.text)

    def test_a_value_that_cannot_be_read_is_shown_as_such(self):
        self.assertIn("cannot be read", url_origin(b"not a real encrypted value"))
        self.assertEqual(url_origin(credentials.encrypt("https://hooks.example.test/a/b?c=d")), "https://hooks.example.test")


if __name__ == "__main__":
    unittest.main()
