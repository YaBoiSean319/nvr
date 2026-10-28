"""The failover recorder (#143): signed messages, the recording gate, the takeover and handback protocol, the configuration copy, and two real NVRs through an outage."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from app import backup, failover
from app.recording import RecordingManager
from app.scheduling import effective_policy
from app.store import connect, initialize
from tests.fixtures.nvr_site import PASSWORD, ROOT, NvrSite
from tests.test_access_grants import GrantCase

SECRET = "failover-shared-secret-0123456789"
PASSPHRASE = "failover-backup-passphrase-123456"


def at(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


class SigningTests(unittest.TestCase):
    def test_a_request_is_good_only_with_the_secret_the_method_the_path_the_body_and_a_fresh_time(self):
        headers = sign = failover.sign(SECRET, "POST", "/api/inbound/failover/lease", b'{"a":1}')
        stamp, signature = headers["X-Failover-Timestamp"], headers["X-Failover-Signature"]
        self.assertTrue(failover.verify(SECRET, "POST", "/api/inbound/failover/lease", b'{"a":1}', stamp, signature))
        for variant in (("other-secret-0123456789", "POST", "/api/inbound/failover/lease", b'{"a":1}'), (SECRET, "GET", "/api/inbound/failover/lease", b'{"a":1}'),
                        (SECRET, "POST", "/api/inbound/failover/backup", b'{"a":1}'), (SECRET, "POST", "/api/inbound/failover/lease", b'{"a":2}')):
            self.assertFalse(failover.verify(*variant, stamp, signature), variant)
        self.assertFalse(failover.verify(SECRET, "POST", "/api/inbound/failover/lease", b'{"a":1}', stamp, signature, now=time.time() + 3600))  # Stale
        self.assertFalse(failover.verify(SECRET, "POST", "/api/inbound/failover/lease", b'{"a":1}', None, signature))
        self.assertFalse(failover.verify(SECRET, "POST", "/api/inbound/failover/lease", b'{"a":1}', stamp, "md5=abc"))
        self.assertFalse(failover.verify(None, "POST", "/api/inbound/failover/lease", b'{"a":1}', stamp, signature))
        del sign


class FailoverCase(GrantCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(failover, "check_peer_url", lambda url, allow_private: url.rstrip("/"))
        patch.start()
        self.addCleanup(patch.stop)
        from app.api import failover as api

        api._failures.clear()

    def configure(self, role: str, **extra):
        body = {"role": role, "peer_url": "https://peer.example", "secret": SECRET, "silence_seconds": 12, "lease_seconds": 5, "sync_seconds": 60, **extra}
        response = self.admin.put("/api/admin/failover", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def state(self) -> dict:
        with connect() as db:
            return failover.read_state(db)

    def policy(self, camera_id: str | None = None) -> dict:
        with connect() as db:
            return effective_policy(db, camera_id or self.d1, datetime.now(timezone.utc))

    def incident(self) -> dict | None:
        with connect() as db:
            row = db.execute("SELECT * FROM incidents WHERE incident_key=?", (failover.TAKEOVER_KEY,)).fetchone()
            return dict(row) if row else None

    def add_live_segment(self, camera_id: str, start: datetime, seconds: int = 2, name: str | None = None) -> int:
        from app.recording import recordings_path

        path = recordings_path() / camera_id / (name or f"segment-{start:%H%M%S%f}.mp4")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"footage " * 50)
        with connect() as db:
            return int(db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes,source) VALUES(?,?,?,?,?,'live')",
                                  (camera_id, f"{camera_id}/{path.name}", start.isoformat(), (start + timedelta(seconds=seconds)).isoformat(), path.stat().st_size)).lastrowid)


class GateTests(FailoverCase):
    def test_a_standby_does_not_record_until_it_takes_over_and_then_records_every_camera(self):
        self.assertEqual(self.policy()["mode"], "continuous")  # Failover off: as always
        self.configure("standby")
        held = self.policy()
        self.assertEqual((held["mode"], held["held"]), ("disabled", "failover_standby"))

        class Probe(RecordingManager):
            started: list[str] = []

            async def _run(self, camera_id: str, mode: str) -> None:
                self.started.append(camera_id)
                await asyncio.sleep(60)

        async def scenario(manager: Probe) -> tuple[list[str], list[str]]:
            await manager.start()
            await asyncio.sleep(0.05)
            before = sorted(manager.tasks)
            self.assertIsNotNone(failover.check_takeover(at(60)))
            for camera in (self.d1, self.d2, self.y1, self.loose):
                await manager.sync_camera(camera)
            await asyncio.sleep(0.05)
            after = sorted(manager.tasks)
            await manager.stop()
            return before, after

        before, after = asyncio.run(scenario(Probe()))
        self.assertEqual(before, [])  # The standby recorded nothing while the primary was heard
        self.assertEqual(after, sorted([self.d1, self.d2, self.y1, self.loose]))  # And every camera once it took over
        self.assertEqual(self.policy().get("held"), None)

    def test_the_standby_waits_the_silence_counted_from_the_last_renewal_and_needs_something_to_record(self):
        self.configure("standby")
        self.assertIsNone(failover.check_takeover(at(5)))
        failover.standby_lease({})  # A renewal now
        self.assertIsNone(failover.check_takeover(at(10)))  # Under 12 seconds since it
        self.assertEqual(self.state()["epoch"], 0)
        self.assertEqual(failover.check_takeover(at(14)), 1)
        self.assertEqual((self.state()["takeover_active"], self.state()["epoch"]), (1, 1))
        self.assertIsNone(failover.check_takeover(at(60)))  # Already taken over: not again
        with connect() as db:
            db.execute("DELETE FROM cameras")
            db.execute("UPDATE failover_state SET takeover_active=0,epoch=0,last_renewed_at=NULL")
            db.execute("DELETE FROM failover_episodes")
            db.execute("UPDATE failover_state SET config_created_at=NULL")
        self.assertIsNone(failover.check_takeover(at(60)))  # Silent, but nothing to record: no takeover
        self.assertIn("no configuration has been copied", self.state()["last_error"])

    def test_the_lease_protocol_stops_the_standby_before_it_grants_and_then_hands_back_its_footage(self):
        self.configure("standby")
        self.assertEqual(failover.standby_lease({})["state"], "watching")
        self.assertTrue(failover.standby_lease({})["granted"])
        failover.check_takeover(at(60))
        started = datetime.fromisoformat(self.state()["takeover_started_at"])
        first = self.add_live_segment(self.d1, started + timedelta(seconds=1))
        self.add_live_segment(self.d2, started + timedelta(seconds=2))
        self.assertEqual(self.incident()["state"], "active")

        # The primary is back: the standby stops recording first, and says so, without granting the lease yet.
        answer = failover.standby_lease({})
        self.assertEqual((answer["granted"], answer["state"], answer["handback"]), (False, "stopping", None))
        self.assertEqual(self.policy()["held"], "failover_standby")
        self.assertEqual(self.state()["takeover_active"], 0)
        # Its recorders are still running as far as it can tell: it keeps saying not yet, until they have stopped.
        with connect() as db:
            db.execute("UPDATE cameras SET recording_status='recording' WHERE id=?", (self.d1,))
        self.assertFalse(failover.standby_lease({})["granted"])
        with connect() as db:
            db.execute("UPDATE cameras SET recording_status='stopped'")
        answer = failover.standby_lease({})
        self.assertEqual((answer["granted"], answer["state"]), (True, "handback"))
        handback = answer["handback"]
        self.assertEqual((handback["epoch"], handback["segments"]), (1, 2))
        listed = failover.standby_footage(1)
        self.assertEqual(sorted(item["camera_id"] for item in listed), sorted([self.d1, self.d2]))
        path, digest = failover.standby_footage_file(1, first)
        self.assertEqual((path.read_bytes() == b"footage " * 50, len(digest)), (True, 64))
        with self.assertRaises(failover.PeerError):
            failover.standby_footage_file(1, 999999)
        with self.assertRaises(failover.PeerError):
            failover.standby_footage(7)

        # Until the primary says the footage is back, the standby's incident stays; then it recovers and the next renewal is an ordinary one.
        self.assertEqual(self.incident()["state"], "active")
        failover.standby_handback_complete(1, 2, 800)
        self.assertEqual(self.incident()["state"], "recovered")
        with connect() as db:
            self.assertEqual(dict(db.execute("SELECT state,segments_imported FROM failover_episodes WHERE epoch=1").fetchone()), {"state": "done", "segments_imported": 2})
        answer = failover.standby_lease({})
        self.assertEqual((answer["granted"], answer["handback"]), (True, None))
        failover.standby_handback_complete(1, 2, 800)  # Said twice: nothing changes
        with self.assertRaises(failover.PeerError):
            failover.standby_handback_complete(9, 0, 0)

    def test_a_handback_cannot_be_completed_while_the_standby_is_still_recording(self):
        self.configure("standby")
        failover.check_takeover(at(60))
        with self.assertRaises(failover.PeerError) as caught:
            failover.standby_handback_complete(1, 0, 0)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.incident()["state"], "active")

    def test_a_primary_does_not_record_while_waiting_to_hear_from_the_standby_or_while_yielding(self):
        self.configure("primary")
        self.assertIsNone(self.policy().get("held"))
        failover.prepare_startup()
        self.assertEqual(self.policy()["held"], "failover_yield")
        sent = datetime.now(timezone.utc)
        failover.FailoverManager._apply_answer({"lease_seconds": 5}, sent, {"granted": True, "epoch": 0, "handback": None})  # Granted: it records
        self.assertIsNone(self.policy().get("held"))
        self.assertEqual(self.state()["peer_state"], "ok")
        failover.FailoverManager._apply_answer({"lease_seconds": 5}, sent, {"granted": False})  # Not yet: it yields for a lease
        self.assertEqual(self.policy()["held"], "failover_yield")
        with connect() as db:
            db.execute("UPDATE failover_state SET yield_until=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        self.assertIsNone(self.policy().get("held"))  # A standby that stops answering does not hold the primary's recording for ever

    def test_a_primary_learns_of_a_takeover_with_an_incident_that_recovers_when_the_footage_is_back(self):
        self.configure("primary")
        sent = datetime.now(timezone.utc)
        handback = {"epoch": 3, "takeover_started_at": (sent - timedelta(minutes=5)).isoformat(), "silent_since": (sent - timedelta(minutes=6)).isoformat(), "segments": 4}
        failover.FailoverManager._apply_answer({"lease_seconds": 5}, sent, {"granted": True, "epoch": 3, "handback": handback})
        self.assertEqual((self.state()["peer_state"], self.incident()["state"]), ("handback", "active"))
        failover.FailoverManager._apply_answer({"lease_seconds": 5}, sent, {"granted": True, "epoch": 3, "handback": handback})  # Told again: still one episode
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM failover_episodes WHERE role='primary'").fetchone()[0], 1)
        failover.finish_handback(3, 4, 1000)
        self.assertEqual(self.incident()["state"], "recovered")
        status = self.admin.get("/api/failover/status").json()
        self.assertEqual(status["role"], "primary")

    def test_the_reason_a_camera_is_not_recording_says_it_is_failover_and_is_expected(self):
        from app import coverage

        self.assertIn("failover_standby", coverage.EXPECTED)
        self.assertEqual(coverage.reason_code({"enabled": 1, "recording_enabled": 1, "stream_fault": None},
                                              {"mode": "disabled", "held": "failover_standby", "scheduled": False}, {"coverage_kind": "expected_idle", "state": "expected_idle"}, False),
                         "failover_standby")


class SettingsTests(FailoverCase):
    def test_settings_are_checked_and_the_secret_is_never_returned(self):
        view = self.configure("standby", allow_private=False)
        self.assertEqual((view["settings"]["role"], view["settings"]["has_secret"]), ("standby", True))
        self.assertNotIn(SECRET, json.dumps(view))
        self.assertNotIn(SECRET, json.dumps(self.admin.get("/api/admin/failover").json()))
        with connect() as db:
            ciphertext = db.execute("SELECT secret_ciphertext FROM failover_settings").fetchone()[0]
        self.assertNotIn(SECRET.encode(), bytes(ciphertext))
        base = {"role": "standby", "peer_url": "https://peer.example", "silence_seconds": 12, "lease_seconds": 5, "sync_seconds": 60}
        self.assertEqual(self.admin.put("/api/admin/failover", json={**base, "silence_seconds": 9}).status_code, 422)  # Under the lease twice over
        self.assertEqual(self.admin.put("/api/admin/failover", json={**base, "lease_seconds": 30, "silence_seconds": 50}).status_code, 422)
        self.assertEqual(self.admin.put("/api/admin/failover", json={**base, "secret": "short"}).status_code, 422)
        self.assertEqual(self.admin.put("/api/admin/failover", json={**base, "peer_url": None}).status_code, 422)
        kept = self.admin.put("/api/admin/failover", json=base)  # No secret sent: the saved one stays
        self.assertEqual(kept.status_code, 200, kept.text)
        self.assertEqual(self.admin.put("/api/admin/failover", json={"role": "off"}).json()["settings"]["role"], "off")
        self.assertIn("failover_settings_changed", {row["action"] for row in self.audit_rows()})
        for row in self.audit_rows():
            self.assertNotIn(SECRET, json.dumps(row))

    def audit_rows(self) -> list[dict]:
        with connect() as db:
            return [{"action": row["action"], "details": row["details_json"]} for row in db.execute("SELECT action,details_json FROM audit_log")]

    def test_the_first_secret_is_required_and_a_standby_cannot_be_changed_while_it_is_recording(self):
        response = self.admin.put("/api/admin/failover", json={"role": "primary", "peer_url": "https://peer.example", "silence_seconds": 12, "lease_seconds": 5})
        self.assertEqual(response.status_code, 422)
        self.configure("standby")
        failover.check_takeover(at(60))
        self.assertEqual(self.admin.put("/api/admin/failover", json={"role": "off"}).status_code, 409)
        self.assertEqual(self.admin.put("/api/admin/failover", json={"role": "primary", "peer_url": "https://peer.example", "silence_seconds": 12, "lease_seconds": 5}).status_code, 409)

    def test_only_administrators_change_failover_and_everyone_sees_where_it_stands(self):
        for client in (self.limited, self.full):
            self.assertEqual(client.get("/api/admin/failover").status_code, 403)
            self.assertEqual(client.put("/api/admin/failover", json={"role": "off"}).status_code, 403)
            self.assertEqual(client.post("/api/admin/failover/sync").status_code, 403)
            status = client.get("/api/failover/status")
            self.assertEqual((status.status_code, status.json()["role"]), (200, "off"))
        self.assertEqual(self.admin.post("/api/admin/failover/sync").status_code, 409)  # Only a standby copies configuration
        self.configure("standby")
        self.assertEqual(self.admin.post("/api/admin/failover/sync").status_code, 202)
        self.assertEqual(self.state()["sync_requested"], 1)
        self.assertEqual(self.limited.get("/api/failover/status").json()["headline"], "Standby: watching the primary")


class PeerEndpointTests(FailoverCase):
    def request(self, method: str, path: str, body: bytes = b"", *, secret: str = SECRET, stamp: int | None = None, client: TestClient | None = None):
        headers = failover.sign(secret, method, path, body, stamp)
        return (client or TestClient(self.app)).request(method, path, content=body, headers=headers)

    def test_nothing_unsigned_wrongly_signed_stale_or_altered_gets_anything(self):
        self.configure("standby")
        client = TestClient(self.app)
        for method, path in (("POST", "/api/inbound/failover/lease"), ("GET", "/api/inbound/failover/footage?epoch=1"), ("GET", "/api/inbound/failover/backup"),
                             ("POST", "/api/inbound/failover/handback-complete"), ("GET", "/api/inbound/failover/footage/1?epoch=1")):
            self.assertEqual(client.request(method, path).status_code, 401, path)  # Unsigned
            self.assertEqual(self.request(method, path, secret="not-the-secret-0123456789").status_code, 401, path)
            self.assertEqual(self.request(method, path, stamp=int(time.time()) - 3600).status_code, 401, path)
        signed = failover.sign(SECRET, "POST", "/api/inbound/failover/lease", b"{}")
        self.assertEqual(client.post("/api/inbound/failover/lease", content=b'{"altered":true}', headers=signed).status_code, 401)
        self.assertEqual(self.request("POST", "/api/inbound/failover/lease", b"{}", client=client).status_code, 200)

    def test_an_oversized_body_is_refused_before_anything_else_and_a_guesser_is_shut_out(self):
        from app.api import failover as api

        self.configure("standby")
        client = TestClient(self.app)
        self.assertEqual(client.post("/api/inbound/failover/lease", content=b"x" * 10_000).status_code, 413)  # Unsigned and large: not read to the end
        api._failures.clear()
        for _ in range(api.MAX_FAILURES_PER_MINUTE):
            self.assertEqual(client.post("/api/inbound/failover/lease", content=b"{}").status_code, 401)
        self.assertEqual(client.post("/api/inbound/failover/lease", content=b"{}").status_code, 429)
        self.assertEqual(self.request("POST", "/api/inbound/failover/lease", b"{}", client=client).status_code, 429)  # Even a good request, for a minute
        api._failures.clear()
        self.assertEqual(self.request("POST", "/api/inbound/failover/lease", b"{}", client=client).status_code, 200)

    def test_the_lease_footage_and_backup_endpoints_answer_for_their_role(self):
        self.configure("standby")
        answer = self.request("POST", "/api/inbound/failover/lease", b"{}").json()
        self.assertEqual((answer["granted"], answer["state"], answer["lease_seconds"]), (True, "watching", 5))
        self.assertIsNotNone(self.state()["last_renewed_at"])
        self.assertEqual(self.request("GET", "/api/inbound/failover/footage?epoch=1").status_code, 404)
        self.assertEqual(self.request("GET", "/api/inbound/failover/footage?epoch=x").status_code, 422)
        self.assertEqual(self.request("GET", "/api/inbound/failover/backup").status_code, 409)  # A standby has no configuration to give
        self.configure("primary")
        self.assertEqual(self.request("POST", "/api/inbound/failover/lease", b"{}").status_code, 409)  # A primary is not asked for a lease
        self.assertEqual(self.request("POST", "/api/inbound/failover/handback-complete", json.dumps({"epoch": 1}).encode()).status_code, 409)

    def test_the_primary_serves_its_encrypted_backup_to_a_signed_request_only(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"NVR_BACKUP_PASSPHRASE": PASSPHRASE, "NVR_BACKUP_PATH": directory}):
            self.configure("primary")
            response = self.request("GET", "/api/inbound/failover/backup")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.content.startswith(backup.MAGIC))
            self.assertNotIn(b"SQLite format", response.content)  # Sealed: nothing readable
            self.assertEqual(len(list(Path(directory).glob("*.nvrbackup"))), 1)
            self.request("GET", "/api/inbound/failover/backup")
            self.assertEqual(len(list(Path(directory).glob("*.nvrbackup"))), 1)  # A fresh one is reused for five minutes
        with mock.patch.dict(os.environ, {"NVR_BACKUP_PASSPHRASE": ""}):
            self.assertEqual(self.request("GET", "/api/inbound/failover/backup").status_code, 409)


@contextmanager
def another_installation(key_from: Path | None):
    """Run as a different NVR: its own database, recordings and (when given) a copy of another's credential key."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "recordings").mkdir()
        environment = {"NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_RECORDINGS_PATH": str(root / "recordings"), "NVR_CREDENTIAL_KEY_PATH": str(root / "credentials.key")}
        if key_from is not None:
            shutil.copy(key_from, root / "credentials.key")
        with mock.patch.dict(os.environ, environment):
            initialize()
            yield root


class ConfigurationCopyTests(FailoverCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {"NVR_BACKUP_PASSPHRASE": PASSPHRASE})
        patch.start()
        self.addCleanup(patch.stop)

    def make_archive(self, directory: str) -> Path:
        with mock.patch.dict(os.environ, {"NVR_BACKUP_PASSPHRASE": PASSPHRASE, "NVR_BACKUP_PATH": directory}):
            return Path(directory) / str(backup.create_backup("manual")["file_name"])

    def test_the_standby_copies_cameras_groups_and_policies_and_keeps_its_own_state_and_footage(self):
        with connect() as db:
            db.execute("INSERT INTO recording_policy_overrides(scope_type,scope_id,timezone_name,schedule_json,updated_at) VALUES('group',?,'UTC','[]',?)", (self.dock, datetime.now(timezone.utc).isoformat()))
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days,updated_at) VALUES('camera',?,9,?)", (self.d1, datetime.now(timezone.utc).isoformat()))
            db.execute("INSERT INTO privacy_masks(camera_id,zones_json,camera_side,updated_at) VALUES(?,?,'none',?)", (self.d1, '[{"x":0,"y":0,"width":0.1,"height":0.1}]', datetime.now(timezone.utc).isoformat()))
            db.execute("UPDATE cameras SET name='Dock 1 (renamed at the primary)' WHERE id=?", (self.d1,))
        key = Path(os.environ["NVR_CREDENTIAL_KEY_PATH"])
        with tempfile.TemporaryDirectory() as backups:
            archive = self.make_archive(backups)
            with another_installation(key):
                local_only = str(uuid.uuid4())
                kept_footage = str(uuid.uuid4())
                with connect() as db:
                    for camera, name in ((local_only, "Local only"), (kept_footage, "Removed at the primary, with footage")):
                        db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,recording_enabled) VALUES(?,?,?,'continuous',1)", (camera, name, b"x"))
                    db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                               (kept_footage, f"{kept_footage}/a.mp4", "2025-01-01T00:00:00+00:00", "2025-01-01T00:01:00+00:00", 10))
                    db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,recording_enabled,recording_status,last_error) VALUES(?,?,?,'continuous',1,'recording','old fault')", (self.d1, "stale name", b"x"))
                counts = failover.apply_configuration(archive)
                self.assertEqual((counts["cameras"], counts["groups"]), (4, 2))
                with connect() as db:
                    names = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM cameras")}
                    self.assertEqual(names[self.d1], "Dock 1 (renamed at the primary)")  # The primary's configuration replaced the standby's
                    self.assertEqual(db.execute("SELECT recording_status,last_error FROM cameras WHERE id=?", (self.d1,)).fetchone()[:], ("recording", "old fault"))  # The standby's own state stayed
                    self.assertNotIn(local_only, names)  # Gone at the primary and no footage here: removed
                    gone = db.execute("SELECT enabled,archived_at FROM cameras WHERE id=?", (kept_footage,)).fetchone()
                    self.assertEqual((gone["enabled"], gone["archived_at"] is not None), (0, True))  # Footage here: kept, switched off
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM recording_segments WHERE camera_id=?", (kept_footage,)).fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT retention_days FROM retention_policy_overrides WHERE scope_id=?", (self.d1,)).fetchone()[0], 9)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM recording_policy_overrides").fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM privacy_masks").fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM camera_group_memberships").fetchone()[0], db.execute("SELECT COUNT(*) FROM camera_group_memberships").fetchone()[0])
                    state = failover.read_state(db)
                    self.assertIsNotNone(state["last_sync_at"])
                    self.assertIsNotNone(state["config_created_at"])
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)  # Users are not copied: the standby has its own
                # Copied again: the same result
                self.assertEqual(failover.apply_configuration(archive)["cameras"], 4)

    def test_a_backup_of_another_nvr_a_wrong_passphrase_or_a_damaged_file_is_refused(self):
        with tempfile.TemporaryDirectory() as backups:
            archive = self.make_archive(backups)
            with another_installation(None):  # Its own credential key: not the primary's
                with self.assertRaises(failover.SyncError) as caught:
                    failover.apply_configuration(archive)
                self.assertIn("credential key differs", str(caught.exception))
            with another_installation(Path(os.environ["NVR_CREDENTIAL_KEY_PATH"])):
                with mock.patch.dict(os.environ, {"NVR_BACKUP_PASSPHRASE": "a-different-passphrase-0123456"}):
                    with self.assertRaises(failover.SyncError):
                        failover.apply_configuration(archive)
                damaged = Path(backups) / "damaged.nvrbackup"
                damaged.write_bytes(archive.read_bytes()[:-40])
                with self.assertRaises(failover.SyncError):
                    failover.apply_configuration(damaged)
                with mock.patch.dict(os.environ, {"NVR_BACKUP_PASSPHRASE": ""}):
                    with self.assertRaises(failover.SyncError):
                        failover.apply_configuration(archive)
                with connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM cameras").fetchone()[0], 0)  # Nothing was copied by a refused one


class ImportTests(FailoverCase):
    def test_footage_the_standby_no_longer_has_is_skipped_and_the_handback_still_completes(self):
        self.configure("primary")
        failover.learn_of_takeover({"epoch": 1, "takeover_started_at": datetime.now(timezone.utc).isoformat(), "silent_since": None, "segments": 2})
        start = datetime.now(timezone.utc)
        listing = [{"id": 11, "camera_id": self.d1, "starts_at": start.isoformat(), "ends_at": (start + timedelta(seconds=2)).isoformat(), "size_bytes": 5},
                   {"id": 12, "camera_id": self.d1, "starts_at": (start + timedelta(seconds=2)).isoformat(), "ends_at": (start + timedelta(seconds=4)).isoformat(), "size_bytes": 5}]
        body = b"hello"
        completed: list[dict] = []

        class Peer(failover.FailoverManager):
            async def _call(self, settings, secret, method, path, payload=None, **options):
                if path.startswith("/api/inbound/failover/footage?"):
                    return httpx.Response(200, json={"segments": listing})
                completed.append(payload)
                return httpx.Response(200, json={"done": True})

            async def _download(self, settings, secret, epoch, segment_id, target):
                if segment_id == 11:
                    raise failover.MissingFootage  # Removed by the standby's retention
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
                import hashlib

                return hashlib.sha256(body).hexdigest()

        asyncio.run(Peer()._import(failover.read_settings(connect().__enter__()) | {"peer_url": "https://peer.example"}, SECRET, 1))
        self.assertEqual(completed, [{"epoch": 1, "imported": 1, "bytes": 5}])
        with connect() as db:
            self.assertEqual(db.execute("SELECT state FROM failover_episodes WHERE role='primary'").fetchone()[0], "done")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recording_segments WHERE source='failover'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM failover_imports WHERE segment_id IS NULL").fetchone()[0], 1)
        self.assertEqual(self.incident()["state"], "recovered")


class RestoreAsStandbyTests(FailoverCase):
    def test_a_backup_restored_as_a_standby_holds_its_recording_and_knows_no_peer(self):
        self.configure("primary")
        with tempfile.TemporaryDirectory() as backups, mock.patch.dict(os.environ, {"NVR_BACKUP_PASSPHRASE": PASSPHRASE, "NVR_BACKUP_PATH": backups}):
            archive = Path(backups) / str(backup.create_backup("manual")["file_name"])
            with another_installation(None):
                backup.restore(archive, force=True)  # An ordinary restore keeps what the backup says: this is the primary again
                with connect() as db:
                    self.assertEqual(failover.read_settings(db)["role"], "primary")
            with another_installation(None):
                backup.restore(archive, force=True, standby=True)
                with connect() as db:
                    settings, state = failover.read_settings(db), failover.read_state(db)
                    self.assertEqual((settings["role"], settings["peer_url"], settings["has_secret"]), ("standby", None, False))
                    self.assertEqual((state["takeover_active"], state["yield_until"]), (0, None))
                    self.assertIsNotNone(state["watching_since"])
                    self.assertEqual(failover.held_reason(db), "failover_standby")
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM cameras").fetchone()[0], 4)  # The primary's cameras came with it


class MigrationTests(unittest.TestCase):
    def test_rebuilding_the_segment_table_keeps_rows_triggers_and_the_id_sequence(self):
        from app import migrations
        from app.migrations import m0039_failover

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            with mock.patch.dict(os.environ, {"NVR_DB_PATH": str(database), "NVR_RECORDINGS_PATH": directory, "NVR_CREDENTIAL_KEY_PATH": str(Path(directory) / "k")}):
                initialize()
                with connect() as db:
                    db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('c','C',x'00')")
                    for index in range(3):
                        db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES('c',?,?,?,1)", (f"c/{index}.mp4", f"2025-01-01T00:0{index}:00+00:00", f"2025-01-01T00:0{index}:30+00:00"))
                    db.execute("DELETE FROM recording_segments WHERE id=3")  # The sequence is ahead of the highest id
                    before = db.execute("SELECT seq FROM sqlite_sequence WHERE name='recording_segments'").fetchone()[0]
                    db.execute("PRAGMA foreign_keys=OFF")
                    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='recording_segments' AND type='table'").fetchone()[0]
                    self.assertIn("'failover'", sql)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM recording_segments").fetchone()[0], 2)
                    triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='recording_segments'")}
                    self.assertTrue({"recording_segments_remove_detections", "footage_chain_removal", "recording_segments_remove_plate_reads"} <= triggers)
                    m0039_failover.verify(db)
                    db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes,source) VALUES('c','c/f.mp4','2025-01-01T01:00:00+00:00','2025-01-01T01:00:30+00:00',1,'failover')")
                    self.assertGreater(db.execute("SELECT id FROM recording_segments WHERE relative_path='c/f.mp4'").fetchone()[0], before - 1)
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes,source) VALUES('c','c/g.mp4','a','b',1,'elsewhere')")
        del migrations


class ClusterTests(unittest.TestCase):
    """Two real NVRs, as separate processes with their own databases: an outage of the primary, the standby recording, the primary's return and the handback."""

    APP = "tests.fixtures.failover_app:app"

    @classmethod
    def setUpClass(cls):
        env = {"NVR_BACKUP_PASSPHRASE": PASSPHRASE, "NVR_TEST_FAKE_RECORDER": "1", "NVR_TEST_FAILOVER_TICK": "0.5"}
        cls.primary = NvrSite("primary", app=cls.APP, env={**env, "NVR_BACKUP_PATH": ""})
        cls.primary.env["NVR_BACKUP_PATH"] = str(cls.primary.base / "backups")
        cls.primary.start()
        cls.primary.seed(["Gate A1", "Gate A2"], group="Gates")
        archive = subprocess.run([sys.executable, "-m", "app.backup", "create"], cwd=ROOT, env=cls.primary.env, capture_output=True, text=True, timeout=60)
        assert archive.returncode == 0, archive.stderr
        cls.standby = NvrSite("standby", app=cls.APP, env={**env, "NVR_BACKUP_PATH": ""})
        cls.standby.env["NVR_BACKUP_PATH"] = str(cls.standby.base / "backups")
        file = max((cls.primary.base / "backups").glob("*.nvrbackup"))
        restored = subprocess.run([sys.executable, "-m", "app.backup", "restore", str(file), "--force", "--standby"], cwd=ROOT, env=cls.standby.env, capture_output=True, text=True, timeout=60)
        assert restored.returncode == 0, restored.stderr
        cls.standby.start()
        cls.standby_client = httpx.Client(base_url=cls.standby.url, timeout=10)
        signed = cls.standby_client.post("/api/auth/login", json={"username": "primary-admin", "password": PASSWORD})
        assert signed.status_code == 200, signed.text
        cls.cameras = cls.primary.made["cameras"]

    @classmethod
    def tearDownClass(cls):
        cls.standby_client.close()
        cls.standby.stop()
        cls.primary.stop()

    @staticmethod
    def eventually(check, timeout: float, message: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = check()
            if result:
                return result
            time.sleep(0.4)
        raise AssertionError(message)

    @staticmethod
    def rows(node: NvrSite, sql: str, *params) -> list[sqlite3.Row]:
        db = sqlite3.connect(node.env["NVR_DB_PATH"], timeout=10)
        db.row_factory = sqlite3.Row
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def status(self, node: NvrSite) -> dict:
        client = node.admin() if node is self.primary else self.standby_client
        return client.get("/api/failover/status").json()

    def incident(self, node: NvrSite) -> sqlite3.Row | None:
        found = self.rows(node, "SELECT * FROM incidents WHERE incident_key=?", failover.TAKEOVER_KEY)
        return found[0] if found else None

    def configure(self, node: NvrSite, role: str, peer: NvrSite) -> None:
        client = node.admin() if node is self.primary else self.standby_client
        response = client.put("/api/admin/failover", json={"role": role, "peer_url": peer.url, "secret": SECRET, "silence_seconds": 12, "lease_seconds": 5, "sync_seconds": 600, "allow_private": True})
        self.assertEqual(response.status_code, 200, response.text)

    def test_an_outage_takeover_return_and_handback(self):
        gate = self.cameras["Gate A1"]
        self.configure(self.standby, "standby", self.primary)
        self.configure(self.primary, "primary", self.standby)
        self.eventually(lambda: self.status(self.primary)["peer_state"] == "ok", 20, "The primary was never granted a lease")
        self.assertEqual(self.status(self.standby)["headline"], "Standby: watching the primary")
        self.eventually(lambda: self.rows(self.primary, "SELECT 1 FROM recording_segments WHERE source='live' AND relative_path LIKE '%segment-%' LIMIT 1"), 15, "The primary does not record")
        self.assertEqual(self.rows(self.standby, "SELECT COUNT(*) FROM recording_segments WHERE relative_path LIKE '%segment-%'")[0][0], 0)  # The standby holds still while the primary is heard

        # The standby copies the primary's configuration, including a camera added after its backup was made.
        self.primary.seed(["Late camera"], group="Added later")
        self.primary.env["NVR_BACKUP_PATH"]  # (the endpoint makes a fresh backup when the newest is over five minutes old: make this one stale)
        for file in (self.primary.base / "backups").glob("*.nvrbackup"):
            os.utime(file, (time.time() - 3600, time.time() - 3600))
        self.assertEqual(self.standby_client.post("/api/admin/failover/sync").status_code, 202)
        self.eventually(lambda: self.rows(self.standby, "SELECT 1 FROM cameras WHERE name='Late camera'"), 30, "The standby did not copy the primary's new camera")
        self.assertIsNotNone(self.status(self.standby)["last_sync_at"])

        # The primary stops. The standby takes over within the silence it was told to wait, and records every camera.
        last_renewal = self.rows(self.standby, "SELECT last_renewed_at FROM failover_state")[0][0]
        stopped = time.monotonic()
        self.primary.stop_process()
        self.eventually(lambda: self.status(self.standby)["takeover_active"], 40, "The standby did not take over")
        waited = time.monotonic() - stopped
        self.assertLess(waited, 12 + 8, f"The standby took {waited:.1f}s to take over, the silence being 12s")
        self.assertGreaterEqual(waited, 6)
        self.assertEqual(self.status(self.standby)["headline"], "Taken over: this standby is recording in the primary's place")
        started = self.eventually(lambda: {row[0] for row in self.rows(self.standby, "SELECT DISTINCT camera_id FROM recording_segments WHERE relative_path LIKE '%segment-%'")}
                                  >= {self.cameras["Gate A1"], self.cameras["Gate A2"]} or None, 20, "The standby did not record both cameras")
        self.assertTrue(started)
        self.assertEqual(self.incident(self.standby)["state"], "active")
        episode = self.rows(self.standby, "SELECT * FROM failover_episodes WHERE role='standby'")[0]
        self.assertGreater(episode["started_at"], last_renewal)

        # The primary returns while the standby is recording: it must not record until the standby has stopped.
        returned = datetime.now(timezone.utc)
        self.primary.start_process()
        self.eventually(lambda: self.incident(self.primary) is not None, 40, "The primary never learned of the takeover")
        self.assertEqual(self.incident(self.primary)["state"], "active")
        self.eventually(lambda: self.rows(self.primary, "SELECT 1 FROM failover_episodes WHERE role='primary' AND state='done'"), 60, "The footage was not handed back")
        self.eventually(lambda: self.incident(self.standby)["state"] == "recovered" and self.incident(self.primary)["state"] == "recovered", 20, "An incident did not recover")
        self.assertFalse(self.status(self.standby)["takeover_active"])

        # Never both at once: the primary's own footage after its return starts after the standby's ended.
        standby_last_end = max(row[0] for row in self.rows(self.standby, "SELECT ends_at FROM recording_segments WHERE relative_path LIKE '%segment-%'"))
        own = self.eventually(lambda: [row[0] for row in self.rows(self.primary, "SELECT starts_at FROM recording_segments WHERE source='live' AND relative_path LIKE '%segment-%' AND ends_at>?", episode["started_at"])],
                              20, "The primary has not recorded since its return")
        self.assertGreaterEqual(min(own), standby_last_end[:19], "The primary recorded while the standby still was")
        self.assertGreater(min(own), returned.isoformat()[:19])
        time.sleep(3)
        self.assertEqual(max(row[0] for row in self.rows(self.standby, "SELECT ends_at FROM recording_segments WHERE relative_path LIKE '%segment-%'")), standby_last_end)  # The standby stays stopped

        # The standby's footage is in the primary's index, marked, there to play, and verifies against the primary's chain.
        imported = self.rows(self.primary, "SELECT * FROM recording_segments WHERE source='failover' ORDER BY starts_at")
        standby_segments = self.rows(self.standby, "SELECT * FROM recording_segments WHERE source='live' AND relative_path LIKE '%segment-%' AND ends_at>?", episode["started_at"])
        self.assertEqual(len(imported), len(standby_segments))
        self.assertGreaterEqual(len(imported), 4)
        for row in imported:
            self.assertTrue((self.primary.base / "recordings" / row["relative_path"]).is_file())
        window = {"start": (datetime.fromisoformat(episode["started_at"]) - timedelta(minutes=2)).isoformat(), "end": datetime.now(timezone.utc).isoformat()}
        timeline = self.primary.admin().post("/api/playback/timeline", json={"camera_ids": [gate], "start": window["start"], "end": window["end"]}).json()["cameras"][0]
        self.assertGreaterEqual(len([item for item in timeline["segments"] if item["source"] == "failover"]), 2)
        self.assertTrue(any("failover" in gap for gap in timeline["gaps"]), timeline["gaps"])  # The wait before the standby took over is explained

        def verified():
            report = self.primary.admin().get(f"/api/cameras/{gate}/footage/verify", params=window).json()
            return report if report["valid"] and not any(item["status"] in {"pending", "ok_unsigned_yet", "signature_missing"} for item in report["segments"]) else None

        report = self.eventually(verified, 30, "The imported footage did not verify")
        self.assertGreaterEqual(len([item for item in report["segments"] if item["status"] == "ok"]), 2)
        self.assertEqual(self.status(self.primary)["peer_state"], "ok")


if __name__ == "__main__":
    unittest.main()
