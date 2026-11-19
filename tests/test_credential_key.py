"""The credential key is read once and kept until its file changes, and a missing key is never replaced on an installation that already holds values encrypted with it (#383)."""
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

    def _camera(self) -> bytes:
        secret = credentials.encrypt("rtsp://viewer:camera-password@camera.example.test/stream")
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('c1','Front gate',?)", (secret,))
        return secret

    def test_a_missing_key_on_an_installation_with_stored_secrets_is_not_replaced(self):
        secret = self._camera()
        original = self.key.read_bytes()
        self.key.unlink()  # A restore of the data without the secrets volume
        with self.assertLogs("nvr.credentials", level="ERROR") as logged:
            with self.assertRaises(credentials.CredentialKeyMissing) as refused:
                credentials.encrypt("a password typed in after the restore")
            with self.assertRaises(credentials.CredentialKeyMissing):
                credentials.decrypt(secret)
            with self.assertRaises(credentials.CredentialKeyMissing):
                credentials.derived_key("api-token")
        self.assertFalse(self.key.exists())
        self.assertEqual(len(logged.output), 1)  # Said once, not on every use
        self.assertIn("CREDENTIAL KEY FILE", logged.output[0])
        self.assertIn("database-recovery.md", logged.output[0])
        for text in (logged.output[0], str(refused.exception)):
            self.assertNotIn("Front gate", text)
            self.assertNotIn("camera-password", text)
        self.assertTrue(credentials.missing())
        self.key.write_bytes(original)  # The original is put back: everything reads again
        self.assertEqual(credentials.decrypt(secret), "rtsp://viewer:camera-password@camera.example.test/stream")
        self.assertFalse(credentials.missing())

    def test_any_encrypted_column_counts_as_a_stored_secret(self):
        credentials.encrypt("first")
        with connect() as db:
            db.execute("CREATE TABLE later_feature(id INTEGER PRIMARY KEY, token_ciphertext BLOB)")
            db.execute("INSERT INTO later_feature(token_ciphertext) VALUES(?)", (credentials.encrypt("a token"),))
        self.key.unlink()
        with self.assertLogs("nvr.credentials", level="ERROR"), self.assertRaises(credentials.CredentialKeyMissing):
            credentials.encrypt("anything")

    def test_a_row_that_holds_no_real_secret_does_not_stop_a_first_key(self):
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('c1','Half made',X'01')")
        with self.assertNoLogs("nvr.credentials", level="ERROR"):
            credentials.encrypt("first")
        self.assertTrue(self.key.exists())

    def test_a_database_that_is_not_there_yet_does_not_stop_a_first_key(self):
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "not-made-yet.sqlite3")
        self.assertEqual(credentials.decrypt(credentials.encrypt("first")), "first")

    def test_start_up_raises_an_incident_and_clears_it_when_the_key_is_back(self):
        self._camera()
        original = self.key.read_bytes()

        def incident():
            with connect() as db:
                return db.execute("SELECT fault_active,summary FROM incidents WHERE incident_key='credential_key'").fetchone()

        credentials.report_at_start()
        self.assertIsNone(incident())  # Nothing wrong, nothing written
        self.key.unlink()
        with self.assertLogs("nvr.credentials", level="ERROR"):
            credentials.report_at_start()
        self.assertEqual(incident()["fault_active"], 1)
        self.assertIn("credential key file is missing", incident()["summary"])
        self.key.write_bytes(original)
        credentials.report_at_start()
        self.assertEqual(incident()["fault_active"], 0)


if __name__ == "__main__":
    unittest.main()
