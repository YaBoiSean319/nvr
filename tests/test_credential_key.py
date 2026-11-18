"""The credential key is read once and kept until its file changes, and making a new key on an installation that already has cameras is said loudly (#383)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import credentials
from app.store import connect, initialize

_ENV = ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH")


class CredentialKeyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in _ENV}
        self.key = Path(self.temporary.name) / "secrets" / "credentials.key"
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(self.key)
        initialize()

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def test_the_key_file_is_read_once_for_many_uses(self):
        credentials.encrypt("first")  # Creates the key
        real = Path.read_bytes
        reads = []

        def counting(path):
            if path == self.key:
                reads.append(path)
            return real(path)

        with patch.object(Path, "read_bytes", counting):
            secrets = [credentials.encrypt(f"rtsp://camera-{number}.example.test/stream") for number in range(50)]
            self.assertEqual([credentials.decrypt(item) for item in secrets][0], "rtsp://camera-0.example.test/stream")
            credentials.derived_key("push-links")
        self.assertEqual(reads, [])

    def test_a_key_file_that_is_replaced_is_read_again(self):
        secret = credentials.encrypt("kept")
        original = self.key.read_bytes()
        self.key.unlink()
        other = credentials.encrypt("under another key")  # A new key is made
        self.assertNotEqual(self.key.read_bytes(), original)
        with self.assertRaises(RuntimeError):
            credentials.decrypt(secret)
        self.key.unlink()
        self.key.write_bytes(original)  # The original is put back
        self.assertEqual(credentials.decrypt(secret), "kept")
        with self.assertRaises(RuntimeError):
            credentials.decrypt(other)

    def test_each_key_file_has_its_own_key(self):
        first = credentials.encrypt("one")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(self.key.with_name("other.key"))
        with self.assertRaises(RuntimeError):
            credentials.decrypt(first)
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(self.key)
        self.assertEqual(credentials.decrypt(first), "one")

    def test_a_first_run_makes_its_key_quietly_with_owner_only_permissions(self):
        with self.assertNoLogs("nvr.credentials", level="ERROR"):
            credentials.encrypt("first")
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)

    def test_a_missing_key_on_an_installation_with_cameras_is_said_loudly(self):
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('c1','Front gate',X'01')")
        with self.assertLogs("nvr.credentials", level="ERROR") as logged:
            credentials.encrypt("after a restore without the secrets volume")
        self.assertIn("CREDENTIAL KEY FILE", logged.output[0])
        self.assertIn("database-recovery.md", logged.output[0])
        self.assertNotIn("Front gate", logged.output[0])


if __name__ == "__main__":
    unittest.main()
