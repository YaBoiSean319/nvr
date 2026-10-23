from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.migrations as migrations
from app import backup
from app.application import create_app
from app.credentials import decrypt, encrypt
from app.principal import COOKIE_NAME
from app.store import connect, initialize
from tests.test_support import TEST_PASSWORD

PASSPHRASE = "correct horse battery staple"
FIXTURES = Path(__file__).parent / "fixtures"


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.backups = root / "backups"
        self.recordings = root / "recordings"
        self.recordings.mkdir()
        self.environment = {
            "NVR_DB_PATH": str(root / "data" / "nvr.sqlite3"),
            "NVR_CREDENTIAL_KEY_PATH": str(root / "secrets" / "credentials.key"),
            "NVR_RECORDINGS_PATH": str(self.recordings),
            "NVR_BACKUP_PATH": str(self.backups),
            "NVR_CONTROL_DIR": str(root / "data" / "run"),
            "NVR_BACKUP_PASSPHRASE": PASSPHRASE,
        }
        self.previous = {key: os.environ.get(key) for key in (*self.environment, "NVR_BACKUP_KEEP", "NVR_STORAGE_VOLUMES")}
        os.environ.update(self.environment)
        os.environ.pop("NVR_STORAGE_VOLUMES", None)
        initialize()

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def _seed(self) -> None:
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'keeper','unused','administrator')")
            db.execute("INSERT INTO camera_groups(id,name) VALUES('g1','Loading dock')")
            db.execute(
                "INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,group_id) VALUES('c1','Dock East',?,'motion','g1')",
                (encrypt("rtsp://camera.invalid/stream?secret=fixture"),),
            )
            db.execute("UPDATE site_branding SET app_name='North Hall' WHERE singleton_id=1")

    def _files(self) -> list[Path]:
        return sorted(self.backups.glob("*.nvrbackup"))

    def test_disabled_without_passphrase_and_never_writes_plaintext(self):
        os.environ.pop("NVR_BACKUP_PASSPHRASE")
        with TestClient(create_app(start_workers=False)) as client:
            self.assertEqual(client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 201)
            state = client.get("/api/backups").json()
            self.assertFalse(state["enabled"])
            self.assertIn("NVR_BACKUP_PASSPHRASE", state["disabled_reason"])
            refused = client.post("/api/backups")
            self.assertEqual(refused.status_code, 409)
        with self.assertRaises(backup.BackupError):
            backup.create_backup("manual")
        self.assertFalse(self.backups.exists() and any(self.backups.iterdir()))
        backup.sync_backup_incident()
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM incidents WHERE fault_type='backup' AND fault_active=1").fetchone())

    def test_requested_backup_runs_in_background_and_is_encrypted(self):
        self._seed()
        with TestClient(create_app()) as client:
            client.cookies.clear()
            with connect() as db:
                db.execute("DELETE FROM users")
            self.assertEqual(client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 201)
            anonymous = TestClient(client.app)  # no second lifespan: one backup manager only
            self.assertEqual(anonymous.post("/api/backups").status_code, 401)
            queued = client.post("/api/backups")
            self.assertEqual(queued.status_code, 202, queued.text)
            run_id = queued.json()["run"]["id"]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                state = client.get("/api/backups").json()
                run = next(run for run in state["runs"] if run["id"] == run_id)
                if run["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.2)
            self.assertEqual(run["status"], "succeeded", run)
            self.assertEqual(state["last_success"]["id"], run_id)
            self.assertFalse(state["stale"])
            # A first start after the daily time also takes its scheduled backup.
            self.assertIn(run["file_name"], [item["file_name"] for item in state["stored"]])
        archive = self.backups / run["file_name"]
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        content = archive.read_bytes()
        for plaintext in (b"SQLite format 3", b"Dock East", b"North Hall", Path(os.environ["NVR_CREDENTIAL_KEY_PATH"]).read_bytes()):
            self.assertNotIn(plaintext, content)
        with connect() as db:
            self.assertEqual(db.execute("SELECT action FROM audit_log WHERE resource_type='backup'").fetchone()[0], "backup_requested")

    def test_tampered_truncated_or_wrongly_unlocked_backups_are_refused(self):
        self._seed()
        archive = self.backups / backup.create_backup("manual")["file_name"]
        original = archive.read_bytes()
        cases = {
            "altered": original[:200] + bytes([original[200] ^ 1]) + original[201:],
            "truncated": original[:-40],
            "trailing": original + b"x",
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(backup.BackupError):
                    backup.decrypt_stream(io.BytesIO(data), io.BytesIO(), PASSPHRASE)
        with self.assertRaisesRegex(backup.BackupError, "Wrong passphrase"):
            backup.decrypt_stream(io.BytesIO(original), io.BytesIO(), "a different passphrase")
        with self.assertRaisesRegex(backup.BackupError, "not an NVR configuration backup"):
            backup.decrypt_stream(io.BytesIO(b"PK\x03\x04" + original), io.BytesIO(), PASSPHRASE)

    def test_restore_round_trip_and_missing_footage_is_marked_unavailable(self):
        self._seed()
        present = self.recordings / "c1" / "present.mp4"
        present.parent.mkdir()
        present.write_bytes(b"footage")
        with connect() as db:
            for name, start, end in (("present", "10:00:00", "10:01:00"), ("missing", "10:01:00", "10:02:00")):
                db.execute(
                    "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES('c1',?,?,?,7)",
                    (f"c1/{name}.mp4", f"2026-10-20T{start}+00:00", f"2026-10-20T{end}+00:00"),
                )
        archive = self.backups / backup.create_backup("manual")["file_name"]

        # Lose the installation: database replaced, key destroyed.
        database = Path(os.environ["NVR_DB_PATH"])
        for leftover in (database, Path(f"{database}-wal"), Path(f"{database}-shm"), Path(os.environ["NVR_CREDENTIAL_KEY_PATH"])):
            leftover.unlink(missing_ok=True)
        initialize()
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cameras").fetchone()[0], 0)

        result = backup.restore(archive)
        self.assertTrue(backup.restore_marker().exists())
        self.assertTrue(result["previous_files"])
        with connect() as db:
            camera = db.execute("SELECT name,stream_url_ciphertext,group_id FROM cameras WHERE id='c1'").fetchone()
            self.assertEqual(db.execute("SELECT app_name FROM site_branding").fetchone()[0], "North Hall")
            self.assertEqual(db.execute("SELECT username FROM users WHERE id=1").fetchone()[0], "keeper")
        self.assertEqual((camera["name"], camera["group_id"]), ("Dock East", "g1"))
        self.assertEqual(decrypt(camera["stream_url_ciphertext"]), "rtsp://camera.invalid/stream?secret=fixture")

        outcome = backup.reconcile_after_restore()
        self.assertEqual(outcome["marked_unavailable"], 1)
        self.assertFalse(backup.restore_marker().exists())
        self.assertIsNone(backup.reconcile_after_restore())

        from app.principal import current_user

        app = create_app(start_workers=False)
        app.dependency_overrides[current_user] = lambda: {"id": 1, "username": "keeper", "role": "administrator"}
        with TestClient(app) as client:
            listed = {segment["starts_at"][11:19]: segment for segment in client.get("/api/cameras/c1/segments").json()["segments"]}
            self.assertTrue(listed["10:00:00"]["available"])
            self.assertFalse(listed["10:01:00"]["available"])
            self.assertIsNone(listed["10:01:00"]["media_url"])
            timeline = client.post("/api/playback/timeline", json={
                "camera_ids": ["c1"], "start": "2026-10-20T10:00:00Z", "end": "2026-10-20T10:02:00Z",
            }).json()["cameras"][0]
        self.assertEqual(len(timeline["segments"]), 1)
        self.assertEqual([(gap["state"], gap["reason"]) for gap in timeline["gaps"]],
                         [("failure", "Footage file was not found after a configuration restore")])

    def test_restore_refuses_a_running_installation_and_unmounted_recordings_defer_reconciliation(self):
        self._seed()
        archive = self.backups / backup.create_backup("manual")["file_name"]
        with connect() as db:
            db.execute("INSERT INTO worker_status(name,state,heartbeat_at) VALUES('recorder','running',?)", (datetime.now(timezone.utc).isoformat(),))
        with self.assertRaisesRegex(backup.BackupError, "appears to be running"):
            backup.restore(archive)
        backup.restore(archive, force=True)
        os.environ["NVR_RECORDINGS_PATH"] = str(self.recordings / "not-mounted")
        self.assertIsNone(backup.reconcile_after_restore())
        self.assertTrue(backup.restore_marker().exists())

    def test_old_backups_are_pruned(self):
        os.environ["NVR_BACKUP_KEEP"] = "2"
        names = [backup.create_backup("manual")["file_name"] for _ in range(4)]
        self.assertEqual([path.name for path in self._files()], names[-2:])

    def test_schedule_runs_once_per_day_after_the_site_time(self):
        def due(moment: str) -> bool:
            return backup.scheduled_run_due(datetime.fromisoformat(moment))

        with connect() as db:
            db.execute("UPDATE site_display_settings SET time_zone='America/Chicago'")
        self.assertTrue(due("2026-10-22T12:00:00+00:00"))
        with connect() as db:
            db.execute("INSERT INTO backup_runs(id,kind,status,created_at) VALUES('s1','scheduled','succeeded','2026-10-22T07:20:00.000Z')")
        self.assertFalse(due("2026-10-22T12:00:00+00:00"))
        # 02:16 Chicago on the next day is still before that day's 02:17 boundary.
        self.assertFalse(due("2026-10-23T07:16:00+00:00"))
        self.assertTrue(due("2026-10-23T07:18:00+00:00"))

    def test_failed_and_stale_backups_raise_an_incident_that_recovers(self):
        self.backups.parent.mkdir(exist_ok=True)
        self.backups.write_text("not a directory")
        run = backup.start_scheduled_run()
        with self.assertRaises(backup.BackupError):
            backup.execute_run(run)
        with connect() as db:
            incident = db.execute("SELECT * FROM incidents WHERE fault_type='backup'").fetchone()
            delivery_type = db.execute(
                "SELECT event_type FROM incident_events WHERE incident_id=?", (incident["id"],)
            ).fetchone()
        self.assertEqual(incident["state"], "active")
        self.assertIn("not writable", incident["summary"])
        self.assertIsNotNone(delivery_type)

        self.backups.unlink()
        backup.execute_run(backup.start_scheduled_run())
        with connect() as db:
            self.assertEqual(db.execute("SELECT state FROM incidents WHERE fault_type='backup'").fetchone()[0], "recovered")
            db.execute("UPDATE backup_runs SET finished_at='2026-01-01T00:00:00.000Z'")
        backup.sync_backup_incident()
        with connect() as db:
            self.assertEqual(db.execute("SELECT state,summary FROM incidents WHERE fault_type='backup'").fetchone()[0], "active")

    def test_pre_migration_hook_backs_up_a_database_before_it_changes(self):
        database = Path(os.environ["NVR_DB_PATH"])
        for leftover in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
            leftover.unlink(missing_ok=True)
        import sqlite3

        legacy = sqlite3.connect(database)
        legacy.executescript((FIXTURES / "legacy_grouped_motion.sql").read_text())
        legacy.close()
        with patch.object(migrations, "pre_migration_hooks", [backup.pre_migration_backup]):
            initialize()
        files = self._files()
        self.assertEqual(len(files), 1)
        self.assertIn("pre-migration", files[0].name)

    def test_cli_restore_reports_errors_without_traceback(self):
        missing = self.backups / "none.nvrbackup"
        self.backups.mkdir(exist_ok=True)
        missing.write_bytes(b"junk")
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(backup.main(["restore", str(missing), "--force"]), 1)
        self.assertIn("not an NVR configuration backup", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
