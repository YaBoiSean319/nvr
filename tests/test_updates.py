"""Guided updates (#148): the release check, what it reports, the pre-update backup, the host commands and the rollback, and that nothing can touch Docker."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import backup, updates
from app.application import create_app
from app.migrations import latest_version
from app.security import hash_password
from app.store import connect
from tests.fixtures.release_feed import ReleaseFeed, release
from tests.test_support import TEST_PASSWORD

PASSPHRASE = "correct horse battery staple"
KEYS = ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_RECORDINGS_PATH", "NVR_BACKUP_PATH", "NVR_CONTROL_DIR", "NVR_BACKUP_PASSPHRASE", "NVR_UPDATE_FEED_URL", "NVR_UPDATE_CHECK",
        "NVR_UPDATE_IMAGE", "NVR_STORAGE_VOLUMES", "NVR_BACKUP_PASSPHRASE_FILE")


class ParseTests(unittest.TestCase):
    def test_only_plain_newer_releases_count(self):
        items = [release("v0.0.9"), release("v9.1.0", draft=True), release("v9.2.0", prerelease=True), release("nightly"), release("v0.2.0"), release("v0.10.0"), "junk", None]
        newest = updates.newest_release(items, (0, 1, 0))
        self.assertEqual(newest.tag, "v0.10.0")  # Numeric, not text, order
        self.assertIsNone(updates.newest_release([release("v0.1.0")], (0, 1, 0)))
        self.assertIsNone(updates.newest_release("not a list"))

    def test_the_marker_gives_the_schema_and_is_left_out_of_the_notes(self):
        found = updates.parse_release(release("v1.0.0", "Fixes and a new tab.", schema=50))
        self.assertEqual((found.schema_version, found.notes), (50, "Fixes and a new tab."))
        self.assertIsNone(updates.parse_release(release("v1.0.0", "No marker")).schema_version)
        bad = dict(release("v1.0.0"), body='<!-- nvr-release: {"schema_version": "x"} -->')
        self.assertIsNone(updates.parse_release(bad).schema_version)

    def test_only_https_or_this_machine_is_a_feed(self):
        self.assertTrue(updates._feed_allowed("https://api.github.com/repos/x/y/releases"))
        self.assertTrue(updates._feed_allowed("http://127.0.0.1:9/releases"))
        self.assertFalse(updates._feed_allowed("http://example.com/releases"))
        self.assertFalse(updates._feed_allowed("ftp://example.com/releases"))
        self.assertFalse(updates._feed_allowed("file:///etc/passwd"))


class UpdateApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in KEYS}
        for key in ("NVR_UPDATE_CHECK", "NVR_STORAGE_VOLUMES", "NVR_BACKUP_PASSPHRASE_FILE"):
            os.environ.pop(key, None)
        (root / "recordings").mkdir()
        os.environ.update({"NVR_DB_PATH": str(root / "data" / "nvr.sqlite3"), "NVR_CREDENTIAL_KEY_PATH": str(root / "secrets" / "credentials.key"),
                           "NVR_RECORDINGS_PATH": str(root / "recordings"), "NVR_BACKUP_PATH": str(root / "backups"), "NVR_CONTROL_DIR": str(root / "data" / "run"),
                           "NVR_BACKUP_PASSPHRASE": PASSPHRASE, "NVR_UPDATE_IMAGE": "ghcr.io/example/nvr"})
        self.feed = ReleaseFeed([release("v0.1.0"), release("v0.2.0", "Two-way audio and a new Updates page.", schema=latest_version() + 1)])
        self.addCleanup(self.feed.close)
        os.environ["NVR_UPDATE_FEED_URL"] = self.feed.url
        context = TestClient(create_app(start_workers=False))
        self.client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.assertEqual(self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 201)

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def audit(self, action: str) -> list[str]:
        with connect() as db:
            return [row["details_json"] for row in db.execute("SELECT details_json FROM audit_log WHERE action=? ORDER BY id", (action,))]

    def test_a_newer_release_is_shown_with_its_notes_and_whether_it_migrates(self):
        before = self.client.get("/api/admin/updates").json()
        self.assertEqual((before["version"], before["schema_version"], before["available"], before["checking"]), (updates.APP_VERSION, latest_version(), None, True))
        self.assertEqual(self.feed.requests, 0)  # Looking at the page asks nobody
        found = self.client.post("/api/admin/updates/check").json()
        self.assertEqual(self.feed.requests, 1)
        available = found["available"]
        self.assertEqual((available["version"], available["notes"], available["has_migrations"]), ("0.2.0", "Two-way audio and a new Updates page.", True))
        self.assertEqual(available["image"], "ghcr.io/example/nvr:0.2.0")
        self.assertIsNotNone(found["last_checked_at"])

    def test_a_release_without_migrations_says_so_and_one_that_does_not_say_is_not_guessed(self):
        self.feed.releases = [release("v0.2.0", "Fixes.", schema=latest_version())]
        self.assertIs(self.client.post("/api/admin/updates/check").json()["available"]["has_migrations"], False)
        self.feed.releases = [release("v0.2.0", "Fixes.")]
        self.assertIsNone(self.client.post("/api/admin/updates/check").json()["available"]["has_migrations"])

    def test_nothing_newer_and_a_broken_feed(self):
        self.feed.releases = [release("v0.1.0")]
        self.assertIsNone(self.client.post("/api/admin/updates/check").json()["available"])
        self.feed.status = 500
        broken = self.client.post("/api/admin/updates/check").json()
        self.assertIn("500", broken["last_error"])
        self.feed.status = 200
        self.feed.releases = [release("v0.3.0")]
        self.assertEqual(self.client.post("/api/admin/updates/check").json()["available"]["version"], "0.3.0")
        self.assertIsNone(self.client.get("/api/admin/updates").json()["last_error"])

    def test_with_the_check_off_no_request_is_made(self):
        saved = self.client.put("/api/admin/updates/settings", json={"check_enabled": False}).json()
        self.assertEqual((saved["check_enabled"], saved["checking"]), (False, False))
        refused = self.client.post("/api/admin/updates/check")
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(self.feed.requests, 0)
        self.client.put("/api/admin/updates/settings", json={"check_enabled": True})
        os.environ["NVR_UPDATE_CHECK"] = "false"  # Off for the whole deployment, whatever the setting says
        state = self.client.get("/api/admin/updates").json()
        self.assertEqual((state["check_enabled"], state["disabled_by_environment"], state["checking"]), (True, True, False))
        self.assertEqual(self.client.post("/api/admin/updates/check").status_code, 409)
        self.assertEqual(self.feed.requests, 0)
        self.assertEqual(len(self.audit("update_check_changed")), 2)

    def test_only_administrators_may_see_or_use_it(self):
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('watcher',?,'operator')", (hash_password(TEST_PASSWORD),))
        with TestClient(self.client.app) as operator:
            self.assertEqual(operator.post("/api/auth/login", json={"username": "watcher", "password": TEST_PASSWORD}).status_code, 200)
            for method, path in (("get", ""), ("post", "/check"), ("post", "/prepare"), ("get", "/plan"), ("put", "/settings")):
                response = getattr(operator, method)(f"/api/admin/updates{path}", **({"json": {"check_enabled": False}} if method == "put" else {}))
                self.assertEqual(response.status_code, 403, (method, path))
        self.assertEqual(self.feed.requests, 0)

    def test_the_steps_come_only_after_the_backup_has_run_and_is_listed(self):
        self.client.post("/api/admin/updates/check")
        self.assertEqual(self.client.get("/api/admin/updates/plan").status_code, 409)  # Nothing started
        started = self.client.post("/api/admin/updates/prepare", json={})
        self.assertEqual(started.status_code, 202, started.text)
        run = started.json()["run"]
        refused = self.client.get("/api/admin/updates/plan")
        self.assertEqual((refused.status_code, "still running" in refused.json()["detail"]), (409, True))  # The backup has not run
        backup.execute_run(run["id"])
        plan = self.client.get("/api/admin/updates/plan")
        self.assertEqual(plan.status_code, 200, plan.text)
        body = plan.json()
        self.assertEqual((body["backup"]["status"], body["backup"]["id"]), ("succeeded", run["id"]))
        self.assertTrue(body["backup"]["file_name"].endswith(".nvrbackup"))
        self.assertTrue((Path(os.environ["NVR_BACKUP_PATH"]) / body["backup"]["file_name"]).exists())
        self.assertIn("image: ghcr.io/example/nvr:0.2.0", "\n".join(body["upgrade"]))
        self.assertIn("docker compose up -d nvr", body["upgrade"])
        self.assertTrue(body["will_migrate"])
        self.assertIn("run on the machine", body["note"])
        self.assertEqual(len(self.audit("update_prepared")), 1)

    def test_a_failed_backup_withholds_the_steps(self):
        self.client.post("/api/admin/updates/check")
        run = self.client.post("/api/admin/updates/prepare", json={}).json()["run"]
        with patch("app.backup.create_backup", side_effect=backup.BackupError("disk full")), self.assertRaises(backup.BackupError):
            backup.execute_run(run["id"])
        refused = self.client.get("/api/admin/updates/plan")
        self.assertEqual(refused.status_code, 409)
        self.assertIn("failed", refused.json()["detail"])

    def test_without_backups_set_up_the_steps_need_an_explicit_choice(self):
        os.environ.pop("NVR_BACKUP_PASSPHRASE")
        self.client.post("/api/admin/updates/check")
        refused = self.client.post("/api/admin/updates/prepare", json={})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("NVR_BACKUP_PASSPHRASE", refused.json()["detail"])
        self.assertEqual(self.client.post("/api/admin/updates/prepare", json={"without_backup": True}).status_code, 202)
        plan = self.client.get("/api/admin/updates/plan").json()
        self.assertEqual((plan["without_backup"], plan["backup"]), (True, None))

    def test_rollback_names_the_pre_migration_copy_made_for_the_upgrade_that_happened(self):
        schema = latest_version()
        database = Path(os.environ["NVR_DB_PATH"])
        earlier = database.with_name(f"{database.name}.pre-migration-v{schema - 2}-to-v{schema - 1}-20261001T100000000000Z")
        this_upgrade = database.with_name(f"{database.name}.pre-migration-v{schema - 1}-to-v{schema}-20261020T100000000000Z")
        earlier.write_bytes(b"older upgrade")
        this_upgrade.write_bytes(b"the copy for this upgrade")
        os.utime(earlier, (1_790_900_000, 1_790_900_000))  # Newer on disk than the one that matters: the choice is by schema, not by age
        os.utime(this_upgrade, (1_790_000_000, 1_790_000_000))
        self.assertIsNone(self.client.get("/api/admin/updates").json()["rollback"])  # Nothing has been upgraded yet
        with connect() as db:
            db.execute("UPDATE update_settings SET seen_version='0.0.9',seen_schema=? WHERE singleton_id=1", (schema - 1,))
        updates.note_start()
        rollback = self.client.get("/api/admin/updates").json()["rollback"]
        self.assertTrue(rollback["has_copy"])
        self.assertEqual(rollback["copy"], this_upgrade.name)
        text = "\n".join(rollback["steps"])
        self.assertIn(this_upgrade.name, text)
        self.assertNotIn(earlier.name, text)
        self.assertIn("image: ghcr.io/example/nvr:0.0.9", text)  # The version it came from
        self.assertIn("docker compose stop nvr", text)

    def test_the_plan_before_an_upgrade_says_its_copy_does_not_exist_yet(self):
        self.client.post("/api/admin/updates/check")
        backup.execute_run(self.client.post("/api/admin/updates/prepare", json={}).json()["run"]["id"])
        rollback = self.client.get("/api/admin/updates/plan").json()["rollback"]
        self.assertFalse(rollback["has_copy"])
        self.assertIn("does not exist yet", "\n".join(rollback["steps"]))
        self.assertIn(f"image: ghcr.io/example/nvr:{updates.APP_VERSION}", "\n".join(rollback["steps"]))
        # A release that does not touch the database needs only the image put back.
        self.feed.releases = [release("v0.2.0", "Fixes.", schema=latest_version())]
        self.client.post("/api/admin/updates/check")
        with connect() as db:
            db.execute("UPDATE update_settings SET prepared_version='0.2.0' WHERE singleton_id=1")
        self.assertNotIn("does not exist yet", "\n".join(self.client.get("/api/admin/updates/plan").json()["rollback"]["steps"]))

    def test_the_steps_are_for_the_release_the_backup_was_taken_for(self):
        self.assertEqual(self.client.post("/api/admin/updates/prepare", json={}).status_code, 409)  # Nothing found yet
        self.client.post("/api/admin/updates/check")
        backup.execute_run(self.client.post("/api/admin/updates/prepare", json={}).json()["run"]["id"])
        self.assertEqual(self.client.get("/api/admin/updates/plan").status_code, 200)
        self.feed.releases = [release("v0.3.0", "Newer still.")]  # A newer release appears after the backup
        self.client.post("/api/admin/updates/check")
        stale = self.client.get("/api/admin/updates/plan")
        self.assertEqual(stale.status_code, 409)
        self.assertIn("0.3.0", stale.json()["detail"])

    def test_a_missing_backup_file_withholds_the_steps(self):
        self.client.post("/api/admin/updates/check")
        run = self.client.post("/api/admin/updates/prepare", json={}).json()["run"]
        backup.execute_run(run["id"])
        for file in Path(os.environ["NVR_BACKUP_PATH"]).glob("*.nvrbackup"):
            file.unlink()
        self.assertEqual(self.client.get("/api/admin/updates/plan").status_code, 409)

    def test_after_the_upgrade_the_release_is_no_longer_offered_and_the_update_is_done(self):
        self.client.post("/api/admin/updates/check")
        backup.execute_run(self.client.post("/api/admin/updates/prepare", json={}).json()["run"]["id"])
        with patch("app.updates.APP_VERSION", "0.2.0"):
            updates.note_start()  # First start of the new version after one that saw 0.1.0 and wrote seen_version below
        with connect() as db:
            db.execute("UPDATE update_settings SET seen_version='0.1.0' WHERE singleton_id=1")
        with patch("app.updates.APP_VERSION", "0.2.0"), patch("app.updates.current_version", lambda: (0, 2, 0)):
            updates.note_start()
            state = self.client.get("/api/admin/updates").json()
        self.assertIsNone(state["available"])
        self.assertIsNone(state["prepared"])

    def test_paths_and_names_in_the_commands_are_quoted_for_a_shell(self):
        import shlex
        copy = {"file": "x", "path": "/data/it's here/$(boom)/nvr.sqlite3.pre-migration-v1-to-v2-20261001T100000000000Z", "from_schema": 1, "to_schema": 2}
        steps = updates.rollback_steps("0.1.0", copy, True)["steps"]
        script = shlex.split([line for line in steps if line.startswith("docker compose run")][0])[-1]
        self.assertEqual(shlex.split(script)[-2], copy["path"])  # One argument, whatever it holds
        os.environ["NVR_UPDATE_IMAGE"] = "ghcr.io/x/y; rm -rf /"
        self.assertEqual(updates.image_repository(), updates.DEFAULT_IMAGE)

    def test_an_upgrade_seen_at_startup_is_listed_with_its_migrations(self):
        with connect() as db:
            db.execute("UPDATE update_settings SET seen_version='0.0.9',seen_schema=? WHERE singleton_id=1", (latest_version() - 1,))
        updates.note_start()
        state = self.client.get("/api/admin/updates").json()
        entry = state["history"][0]
        self.assertEqual((entry["from_version"], entry["to_version"], entry["from_schema"], entry["to_schema"]), ("0.0.9", updates.APP_VERSION, latest_version() - 1, latest_version()))
        self.assertEqual([item["version"] for item in entry["migrations"]], [latest_version()])
        self.assertEqual(state["applied_migrations"][0]["version"], latest_version())
        updates.note_start()  # A start that changes nothing adds nothing
        self.assertEqual(len(self.client.get("/api/admin/updates").json()["history"]), 1)
        self.assertEqual(len(self.audit("version_changed")), 1)


class NoDockerTests(unittest.TestCase):
    def test_no_route_can_pull_an_image_or_restart_a_container(self):
        app = create_app(start_workers=False)
        paths = {(route.path, tuple(sorted(route.methods or ()))) for route in app.routes if getattr(route, "path", "").startswith("/api/admin/updates")}
        for route in app.routes:  # No route anywhere is named for pulling or restarting
            self.assertNotRegex(getattr(route, "path", ""), r"(?i)docker|restart|/pull\b|/upgrade/run")
        self.assertEqual({path for path, _ in paths}, {"/api/admin/updates", "/api/admin/updates/settings", "/api/admin/updates/check", "/api/admin/updates/prepare", "/api/admin/updates/plan"})
        for source in ("app/updates.py", "app/api/updates.py"):
            text = (Path(__file__).resolve().parent.parent / source).read_text(encoding="utf-8")
            code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", '"""')))
            for forbidden in ("subprocess", "os.system", "docker.sock", "popen", "/var/run/docker"):
                self.assertNotIn(forbidden, code.replace("`docker compose", ""), (source, forbidden))

    def test_the_compose_files_never_mount_the_docker_socket(self):
        root = Path(__file__).resolve().parent.parent
        for path in root.glob("compose*.y*ml"):
            self.assertNotIn("docker.sock", path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
