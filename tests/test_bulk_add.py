"""Bulk camera adding as a job the server runs (#173)."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import bulk_add
from app.application import create_app
from app.security import hash_password
from tests.test_support import TEST_PASSWORD

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_COOKIE_SECURE")
PASSWORD = "S3cret-Passw0rd!-for-camera"


def report(ok: bool = True, *, make: str = "Hanwha", model: str = "XNV-6120R", record: str = "profile2", live: str | None = "profile4", live_verified: bool = True,
           failed_step: tuple[str, str, str] | None = None) -> dict:
    steps = [{"id": "connect", "label": "Connecting to camera", "state": "ok", "detail": ""}, {"id": "ready", "label": "Camera ready", "state": "ok" if ok else "failed", "detail": ""}]
    if failed_step:
        steps.insert(1, {"id": failed_step[0], "label": failed_step[1], "state": "failed", "detail": failed_step[2]})
    return {"ok": ok, "brand": "hanwha", "device_service_url": "http://camera.example/onvif/device_service", "device": {"manufacturer": make, "model": model},
            "steps": steps, "selection": {"record_token": record, "live_token": live, "live_verified": live_verified}}


class HelperTests(unittest.TestCase):
    def test_an_address_with_a_port_names_the_onvif_service_there(self):
        self.assertEqual(bulk_add.split_address("192.0.2.10"), ("192.0.2.10", None))
        self.assertEqual(bulk_add.split_address("  camera.example  "), ("camera.example", None))
        self.assertEqual(bulk_add.split_address("192.0.2.10:8080"), ("192.0.2.10", "http://192.0.2.10:8080/onvif/device_service"))
        self.assertEqual(bulk_add.split_address("[2001:db8::1]:8080"), ("2001:db8::1", "http://[2001:db8::1]:8080/onvif/device_service"))
        self.assertEqual(bulk_add.split_address("[2001:db8::1]"), ("2001:db8::1", None))
        self.assertEqual(bulk_add.split_address("192.0.2.10:99999"), ("192.0.2.10:99999", None))

    def test_a_camera_is_named_from_its_make_and_model_and_the_address_it_was_given(self):
        self.assertEqual(bulk_add.suggested_name(report(), "192.0.2.10:8080"), "Hanwha XNV-6120R 192.0.2.10:8080")
        self.assertEqual(bulk_add.suggested_name(report(make="", model=""), "192.0.2.10"), "Camera 192.0.2.10")
        self.assertEqual(bulk_add.suggested_name(None, "192.0.2.10"), "Camera 192.0.2.10")

    def test_what_needs_attention_is_the_first_failed_step(self):
        self.assertEqual(bulk_add.attention_text(report(False, failed_step=("video", "Verifying video", "No frame could be decoded"))), "Verifying video: No frame could be decoded")
        self.assertEqual(bulk_add.attention_text(report(False)), "Video could not be verified")


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        os.environ["NVR_COOKIE_SECURE"] = "false"
        self.app = create_app(start_workers=False)  # The runner is driven by the tests, not by its own loop
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()
        self.client.post("/api/setup/admin", json={"username": "bulk-admin", "password": TEST_PASSWORD})
        self.added: list = []

        async def fake_add(payload, recorders, **kwargs):
            self.added.append(payload)
            number = len(self.added)
            await asyncio.sleep(0)
            return {"id": f"camera-{number}", "name": payload.name}

        self.fake_add = fake_add

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temporary.cleanup()
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def start(self, cameras, **options) -> dict:
        response = self.client.post("/api/cameras/bulk-jobs", json={"cameras": cameras, **options})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def work(self, inspect=None, add=None):
        inspect = inspect or AsyncMock(return_value=report())
        with patch.object(bulk_add.camera_inspect, "inspect_camera", new=inspect), patch("app.api.cameras.add_camera", new=add or self.fake_add):
            asyncio.run(self.app.state.bulk_add.run_until_idle())
        return inspect

    def job(self, job_id: str) -> dict:
        return self.client.get(f"/api/cameras/bulk-jobs/{job_id}").json()

    def rows(self, sql: str, *params):
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.row_factory = sqlite3.Row
            return db.execute(sql, params).fetchall()

    # --- the API -----------------------------------------------------------------------------------------------------------------

    def test_only_administrators_can_start_watch_or_stop_a_job(self):
        job = self.start([{"host": "192.0.2.10", "username": "admin", "password": PASSWORD}])
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,?)", ("bulk-operator", hash_password(TEST_PASSWORD), "operator"))
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/cameras/bulk-jobs", json={"cameras": [{"host": "192.0.2.11"}]}).status_code, 401)
        self.client.post("/api/auth/login", json={"username": "bulk-operator", "password": TEST_PASSWORD})
        self.assertEqual(self.client.post("/api/cameras/bulk-jobs", json={"cameras": [{"host": "192.0.2.11"}]}).status_code, 403)
        self.assertEqual(self.client.get(f"/api/cameras/bulk-jobs/{job['id']}").status_code, 403)
        self.assertEqual(self.client.get("/api/cameras/bulk-jobs/latest").status_code, 403)
        self.assertEqual(self.client.post(f"/api/cameras/bulk-jobs/{job['id']}/cancel").status_code, 403)

    def test_a_list_must_be_sensible(self):
        for body in ({"cameras": []}, {"cameras": [{"host": ""}]}, {"cameras": [{"host": "192.0.2.10", "username": "admin"}]},
                     {"cameras": [{"host": "192.0.2.10", "password": "x"}]}, {"cameras": [{"host": "192.0.2.%d" % (n % 250)} for n in range(bulk_add.MAX_CAMERAS + 1)]}):
            self.assertEqual(self.client.post("/api/cameras/bulk-jobs", json=body).status_code, 422, body)
        self.assertEqual(self.client.get("/api/cameras/bulk-jobs/nonexistent").status_code, 404)
        self.assertEqual(self.client.post("/api/cameras/bulk-jobs/nonexistent/cancel").status_code, 404)

    def test_a_job_shows_its_cameras_in_order_and_never_a_password(self):
        job = self.start([{"host": "192.0.2.10", "username": "admin", "password": PASSWORD, "name": "Gate"}, {"host": "192.0.2.11:8080"}])
        self.assertEqual(job["state"], "running")
        self.assertEqual([(item["row"], item["host"], item["name"], item["status"]) for item in job["items"]], [(1, "192.0.2.10", "Gate", "queued"), (2, "192.0.2.11:8080", "", "queued")])
        self.assertEqual(job["counts"], {"queued": 2})
        self.assertNotIn(PASSWORD, json.dumps(job))
        self.assertEqual(self.client.get("/api/cameras/bulk-jobs/latest").json()["job"]["id"], job["id"])
        stored = self.rows("SELECT password_ciphertext FROM camera_add_items WHERE row_number=1")[0][0]
        self.assertIsNotNone(stored)
        self.assertNotIn(PASSWORD.encode(), stored)  # Encrypted at rest
        audit = json.dumps([dict(row) for row in self.rows("SELECT * FROM audit_log")])
        self.assertNotIn(PASSWORD, audit)
        self.assertIn("camera_bulk_add_started", audit)

    # --- the work ----------------------------------------------------------------------------------------------------------------

    def test_each_camera_is_checked_and_added_as_the_form_would_add_it_and_its_password_is_then_cleared(self):
        job = self.start([{"host": "192.0.2.10:8080", "username": "admin", "password": PASSWORD}, {"host": "192.0.2.11", "username": "admin", "password": PASSWORD, "name": "Dock"}], recording=True)
        inspect = self.work()
        self.assertEqual(inspect.await_count, 2)
        # Two cameras are worked on at once, so which is saved first is not fixed: each is found by its address.
        first, second = sorted(self.added, key=lambda payload: payload.camera_ip)
        self.assertEqual((first.name, first.camera_ip, first.camera_username, first.camera_password), ("Hanwha XNV-6120R 192.0.2.10:8080", "192.0.2.10", "admin", PASSWORD))
        self.assertEqual((first.camera_brand, first.onvif_profile_token, first.live_substream_profile_token), ("hanwha", "profile2", "profile4"))
        self.assertEqual((first.recording_mode, first.recording_enabled, first.save_anyway), ("motion", True, False))
        self.assertEqual(second.name, "Dock")
        # The address's port reached the inspection as the ONVIF service address, the way Quick Add sends it.
        self.assertEqual(inspect.await_args_list[0].kwargs["device_service_url"], "http://192.0.2.10:8080/onvif/device_service")
        view = self.job(job["id"])
        self.assertEqual((view["state"], view["counts"]), ("done", {"added": 2}))
        self.assertTrue(view["finished_at"])
        self.assertEqual(sorted(item["camera_id"] for item in view["items"]), ["camera-1", "camera-2"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM camera_add_items WHERE password_ciphertext IS NOT NULL")[0][0], 0)
        self.assertEqual(self.client.get("/api/cameras/bulk-jobs/latest").json()["job"]["state"], "done")  # Still there to be looked at

    def test_a_camera_that_needs_attention_is_added_with_recording_off_or_not_added_as_asked(self):
        needing = AsyncMock(return_value=report(False, failed_step=("video", "Verifying video", "No frame could be decoded"), live=None))
        job = self.start([{"host": "192.0.2.10", "username": "admin", "password": PASSWORD}], recording=True, add_anyway=True)
        self.work(inspect=needing)
        payload = self.added[0]
        self.assertEqual((payload.save_anyway, payload.recording_enabled), (True, False))
        item = self.job(job["id"])["items"][0]
        self.assertEqual(item["status"], "attention")
        self.assertIn("Verifying video: No frame could be decoded", item["detail"])
        self.added.clear()
        job = self.start([{"host": "192.0.2.11", "username": "admin", "password": PASSWORD}], add_anyway=False)
        self.work(inspect=needing)
        self.assertEqual(self.added, [])
        item = self.job(job["id"])["items"][0]
        self.assertEqual(item["status"], "failed")
        self.assertTrue(item["detail"].startswith("Not added because it needs attention: Verifying video"))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM camera_add_items WHERE password_ciphertext IS NOT NULL")[0][0], 0)

    def test_an_unverified_live_stream_is_not_used(self):
        self.start([{"host": "192.0.2.10"}])
        self.work(inspect=AsyncMock(return_value=report(live_verified=False)))
        self.assertIsNone(self.added[0].live_substream_profile_token)

    def test_one_cameras_failure_is_that_cameras_result_and_the_rest_are_still_added(self):
        async def add(payload, recorders, **kwargs):
            if payload.camera_ip == "192.0.2.11":
                raise HTTPException(status_code=409, detail="A camera with that name or ONVIF identity already exists")
            return await self.fake_add(payload, recorders)

        async def inspect(host, *args, **kwargs):
            if host == "192.0.2.12":
                raise ValueError("Link-local addresses are not used as camera addresses")
            return report()

        job = self.start([{"host": "192.0.2.10"}, {"host": "192.0.2.11"}, {"host": "192.0.2.12"}, {"host": "192.0.2.13"}])
        self.work(inspect=AsyncMock(side_effect=inspect), add=add)
        view = self.job(job["id"])
        self.assertEqual([item["status"] for item in view["items"]], ["added", "failed", "failed", "added"])
        self.assertIn("already exists", view["items"][1]["detail"])
        self.assertIn("Link-local", view["items"][2]["detail"])
        self.assertEqual(view["state"], "done")

    def test_a_bad_login_for_one_row_is_reported_without_the_password(self):
        job = self.start([{"host": "192.0.2.10", "username": "admin", "password": PASSWORD}])
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("UPDATE camera_add_items SET password_ciphertext=NULL")  # A row with a username and no password
        self.work()
        item = self.job(job["id"])["items"][0]
        self.assertEqual(item["status"], "failed")
        self.assertIn("both camera username and password", item["detail"])
        self.assertNotIn(PASSWORD, json.dumps(self.job(job["id"])))

    def test_at_most_three_cameras_are_checked_at_once(self):
        running = {"now": 0, "most": 0}

        async def inspect(*args, **kwargs):
            running["now"] += 1
            running["most"] = max(running["most"], running["now"])
            await asyncio.sleep(0.05)
            running["now"] -= 1
            return report()

        job = self.start([{"host": f"192.0.2.{n}"} for n in range(10, 20)])
        self.work(inspect=AsyncMock(side_effect=inspect))
        self.assertEqual(running["most"], bulk_add.CONCURRENCY)
        self.assertEqual(self.job(job["id"])["counts"], {"added": 10})

    def test_stopping_a_job_skips_what_has_not_started_clears_its_passwords_and_lets_the_running_ones_finish(self):
        job = self.start([{"host": f"192.0.2.{n}", "username": "admin", "password": PASSWORD} for n in range(10, 16)])
        claimed = bulk_add.claim(2)  # Two are being checked when Stop is pressed
        self.assertEqual(len(claimed), 2)
        cancelled = self.client.post(f"/api/cameras/bulk-jobs/{job['id']}/cancel").json()
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["counts"], {"checking": 2, "skipped": 4})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM camera_add_items WHERE status='skipped' AND password_ciphertext IS NOT NULL")[0][0], 0)
        with patch.object(bulk_add.camera_inspect, "inspect_camera", new=AsyncMock(return_value=report())), patch("app.api.cameras.add_camera", new=self.fake_add):
            async def finish():
                await asyncio.gather(*(bulk_add.process(item, None) for item in claimed))

            asyncio.run(finish())
        view = self.job(job["id"])
        self.assertEqual(view["counts"], {"added": 2, "skipped": 4})
        self.assertEqual(view["state"], "cancelled")  # Stays stopped
        self.assertEqual(len(self.added), 2)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM camera_add_items WHERE password_ciphertext IS NOT NULL")[0][0], 0)

    def test_after_a_restart_a_camera_only_being_checked_is_queued_again_and_one_being_saved_is_reported(self):
        job = self.start([{"host": "192.0.2.10", "username": "admin", "password": PASSWORD}, {"host": "192.0.2.11", "username": "admin", "password": PASSWORD}])
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("UPDATE camera_add_items SET status='checking' WHERE row_number=1")
            db.execute("UPDATE camera_add_items SET status='adding' WHERE row_number=2")
        bulk_add.recover_interrupted()
        view = self.job(job["id"])
        self.assertEqual([item["status"] for item in view["items"]], ["queued", "failed"])
        self.assertIn("check the camera list", view["items"][1]["detail"])
        self.assertEqual(self.rows("SELECT password_ciphertext IS NULL FROM camera_add_items WHERE row_number=2")[0][0], 1)
        self.work()
        self.assertEqual(self.job(job["id"])["state"], "done")

    def test_after_a_restart_a_camera_being_checked_in_a_cancelled_job_is_skipped_and_its_password_is_gone(self):
        job = self.start([{"host": "192.0.2.10", "username": "admin", "password": PASSWORD}])
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:  # Cancelled while its one camera was being checked, and then the server stopped
            db.execute("UPDATE camera_add_items SET status='checking'")
            db.execute("UPDATE camera_add_jobs SET state='cancelled'")
        bulk_add.recover_interrupted()
        view = self.job(job["id"])
        self.assertEqual([item["status"] for item in view["items"]], ["skipped"])  # It was put back in the queue of a job nobody works (#386)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM camera_add_items WHERE password_ciphertext IS NOT NULL")[0][0], 0)

    # --- several discovered devices with one login (#173) ------------------------------------------------------------------------

    def discovered(self, candidate_id: str, device_uuid: str, service_url: str, *, linked: str | None = None, claimed_until: str | None = None) -> str:
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            if linked:
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (linked, f"Existing {linked}", b""))
            db.execute("INSERT INTO discovery_candidates(id,device_uuid,service_urls,scopes,capabilities,linked_camera_id,add_claim_token,add_claimed_until) "
                       "VALUES(?,?,?,'[]','{}',?,?,?)",
                       (candidate_id, device_uuid, json.dumps([service_url]), linked, "someone-else" if claimed_until else None, claimed_until))
        return candidate_id

    def start_discovered(self, candidate_ids, **options):
        return self.client.post("/api/cameras/bulk-jobs/discovered", json={"candidate_ids": candidate_ids, "username": "admin", "password": PASSWORD, **options})

    def linking_add(self):
        """add_camera as it treats a discovery claim: the new camera is linked to the record the claim holds, and the claim ends."""
        claims: list = []

        async def add(payload, recorders, *, discovery_claim=None):
            claims.append(discovery_claim)
            camera = await self.fake_add(payload, recorders)
            if discovery_claim:
                with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
                    linked = db.execute("UPDATE discovery_candidates SET linked_camera_id=?,add_claim_token=NULL,add_claimed_until=NULL WHERE id=? AND add_claim_token=?",
                                        (camera["id"], discovery_claim["candidate_id"], discovery_claim["token"])).rowcount
                self.assertEqual(linked, 1, "the claim given to add_camera was not the one held")
            return camera

        return add, claims

    def test_discovered_devices_are_added_with_one_login_from_their_discovery_records(self):
        gate = self.discovered("cand-gate", "5f1c0a52-0000-4000-8000-000000000001", "http://192.0.2.20:8080/onvif/device_service")
        dock = self.discovered("cand-dock", "ipscan:192.0.2.21", "http://192.0.2.21/onvif/device_service")
        response = self.start_discovered([gate, dock], recording=True)
        self.assertEqual(response.status_code, 201, response.text)
        job = response.json()
        self.assertEqual([(item["row"], item["host"], item["status"]) for item in job["items"]], [(1, "192.0.2.20:8080", "queued"), (2, "192.0.2.21", "queued")])
        self.assertNotIn(PASSWORD, json.dumps(job))
        add, claims = self.linking_add()
        inspect = self.work(add=add)
        # Each device is reached at the ONVIF service its discovery record holds, with the shared login.
        calls = sorted((call.args[0], call.args[1], call.args[2], call.kwargs["device_service_url"]) for call in inspect.await_args_list)
        self.assertEqual(calls, [("192.0.2.20", "admin", PASSWORD, "http://192.0.2.20:8080/onvif/device_service"),
                                 ("192.0.2.21", "admin", PASSWORD, "http://192.0.2.21/onvif/device_service")])
        saved = {payload.camera_ip: payload for payload in self.added}
        self.assertEqual(saved["192.0.2.20"].onvif_uuid, "5f1c0a52-0000-4000-8000-000000000001")  # Its ONVIF identity is kept
        self.assertIsNone(saved["192.0.2.21"].onvif_uuid)  # A device found by address has none
        self.assertEqual(saved["192.0.2.20"].name, "Hanwha XNV-6120R 192.0.2.20:8080")
        self.assertTrue(all(payload.recording_enabled for payload in self.added))
        self.assertEqual(sorted(claim["candidate_id"] for claim in claims), [dock, gate])
        links = {row["id"]: (row["linked_camera_id"], row["add_claim_token"]) for row in self.rows("SELECT id,linked_camera_id,add_claim_token FROM discovery_candidates")}
        self.assertTrue(all(camera and token is None for camera, token in links.values()), links)  # Both linked to their new cameras
        view = self.job(job["id"])
        self.assertEqual((view["state"], view["counts"]), ("done", {"added": 2}))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM camera_add_items WHERE password_ciphertext IS NOT NULL")[0][0], 0)
        started = self.rows("SELECT details_json FROM audit_log WHERE action='camera_bulk_add_started'")
        self.assertEqual(json.loads(started[0][0])["discovered"], 2)
        self.assertNotIn(PASSWORD, json.dumps([tuple(row) for row in self.rows("SELECT * FROM audit_log")]))

    def test_a_choice_of_discovered_devices_must_still_be_valid(self):
        gate = self.discovered("cand-gate", "5f1c0a52-0000-4000-8000-000000000001", "http://192.0.2.20/onvif/device_service")
        self.discovered("cand-done", "5f1c0a52-0000-4000-8000-000000000002", "http://192.0.2.22/onvif/device_service", linked="camera-old")
        missing = self.start_discovered([gate, "cand-forgotten"])
        self.assertEqual(missing.status_code, 404)
        self.assertIn("1 of the selected devices are no longer in the discovery list", missing.json()["detail"])
        already = self.start_discovered([gate, "cand-done"])
        self.assertEqual(already.status_code, 409)
        self.assertIn("already cameras", already.json()["detail"])
        for body in ({"candidate_ids": []}, {"candidate_ids": [gate, gate]}, {"candidate_ids": [gate], "username": "admin"}, {"candidate_ids": [gate], "password": "x"}):
            self.assertEqual(self.client.post("/api/cameras/bulk-jobs/discovered", json=body).status_code, 422, body)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM camera_add_jobs")[0][0], 0)  # Nothing was started
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,?)", ("bulk-operator", hash_password(TEST_PASSWORD), "operator"))
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "bulk-operator", "password": TEST_PASSWORD})
        self.assertEqual(self.start_discovered([gate]).status_code, 403)

    def test_a_discovered_device_being_added_elsewhere_is_reported_and_its_claim_left_alone(self):
        later = (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat()
        busy = self.discovered("cand-busy", "5f1c0a52-0000-4000-8000-000000000003", "http://192.0.2.23/onvif/device_service", claimed_until=later)
        job = self.start_discovered([busy]).json()
        inspect = self.work()
        self.assertEqual(inspect.await_count, 0)
        item = self.job(job["id"])["items"][0]
        self.assertEqual(item["status"], "failed")
        self.assertIn("already being added", item["detail"])
        self.assertEqual(self.rows("SELECT add_claim_token FROM discovery_candidates WHERE id=?", busy)[0][0], "someone-else")

    def test_a_discovered_device_that_is_not_added_is_let_go(self):
        refused = self.discovered("cand-refused", "5f1c0a52-0000-4000-8000-000000000004", "http://192.0.2.24/onvif/device_service")
        unwell = self.discovered("cand-unwell", "5f1c0a52-0000-4000-8000-000000000005", "http://192.0.2.25/onvif/device_service")

        async def refuse(payload, recorders, **kwargs):
            raise HTTPException(status_code=422, detail="Camera stream could not be opened")

        job = self.start_discovered([refused]).json()
        self.work(add=refuse)
        self.assertEqual(self.job(job["id"])["items"][0]["status"], "failed")
        job = self.start_discovered([unwell], add_anyway=False).json()
        self.work(inspect=AsyncMock(return_value=report(False)))
        self.assertIn("needs attention", self.job(job["id"])["items"][0]["detail"])
        # Neither stays claimed or linked: either can be added again at once, here or by hand.
        rows = self.rows("SELECT linked_camera_id,add_claim_token,add_claimed_until FROM discovery_candidates WHERE id IN (?,?)", refused, unwell)
        self.assertEqual([tuple(row) for row in rows], [(None, None, None), (None, None, None)])

    def test_after_a_restart_a_discovered_device_being_checked_is_let_go_and_queued_again(self):
        gate = self.discovered("cand-gate", "5f1c0a52-0000-4000-8000-000000000006", "http://192.0.2.26/onvif/device_service")
        job = self.start_discovered([gate]).json()
        later = (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat()
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:  # The server stopped while it was being checked, holding the claim
            db.execute("UPDATE camera_add_items SET status='checking'")
            db.execute("UPDATE discovery_candidates SET add_claim_token='job-claim',add_claimed_until=?", (later,))
        bulk_add.recover_interrupted()
        self.assertEqual(self.rows("SELECT add_claim_token FROM discovery_candidates")[0][0], None)
        add, _ = self.linking_add()
        self.work(add=add)
        self.assertEqual(self.job(job["id"])["counts"], {"added": 1})

    def test_finished_jobs_are_forgotten_after_a_week(self):
        old = self.start([{"host": "192.0.2.10"}])
        self.work()
        long_ago = (datetime.now(timezone.utc) - timedelta(days=bulk_add.KEEP_DAYS + 1)).isoformat()
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("UPDATE camera_add_jobs SET finished_at=? WHERE id=?", (long_ago, old["id"]))
        recent = self.start([{"host": "192.0.2.11"}])  # Starting a job tidies the old ones away
        self.assertEqual(self.client.get(f"/api/cameras/bulk-jobs/{old['id']}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/cameras/bulk-jobs/{recent['id']}").status_code, 200)

    def test_the_latest_job_is_the_running_one_or_one_finished_within_the_hour(self):
        self.assertEqual(self.client.get("/api/cameras/bulk-jobs/latest").json(), {"job": None})
        job = self.start([{"host": "192.0.2.10"}])
        self.assertEqual(self.client.get("/api/cameras/bulk-jobs/latest").json()["job"]["id"], job["id"])
        self.work()
        self.assertEqual(self.client.get("/api/cameras/bulk-jobs/latest").json()["job"]["state"], "done")
        long_ago = (datetime.now(timezone.utc) - timedelta(hours=bulk_add.RECENT_HOURS + 1)).isoformat()
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("UPDATE camera_add_jobs SET finished_at=?", (long_ago,))
        self.assertEqual(self.client.get("/api/cameras/bulk-jobs/latest").json(), {"job": None})

    def test_a_list_of_five_hundred_is_worked_through_three_at_a_time(self):
        running = {"now": 0, "most": 0}

        async def inspect(*args, **kwargs):
            running["now"] += 1
            running["most"] = max(running["most"], running["now"])
            await asyncio.sleep(0)
            running["now"] -= 1
            return report()

        job = self.start([{"host": f"10.9.{n // 250}.{n % 250 + 1}", "name": f"Scale {n + 1}"} for n in range(bulk_add.MAX_CAMERAS)])
        self.work(inspect=AsyncMock(side_effect=inspect))
        self.assertLessEqual(running["most"], bulk_add.CONCURRENCY)
        view = self.job(job["id"])
        self.assertEqual((view["state"], view["counts"]), ("done", {"added": bulk_add.MAX_CAMERAS}))
        self.assertEqual(len({item["name"] for item in view["items"]}), bulk_add.MAX_CAMERAS)

    def test_the_runner_works_through_a_job_on_its_own_when_started(self):
        job = self.start([{"host": "192.0.2.10"}, {"host": "192.0.2.11"}])

        async def body():
            runner = bulk_add.BulkAddRunner(None)
            with patch.object(bulk_add.camera_inspect, "inspect_camera", new=AsyncMock(return_value=report())), patch("app.api.cameras.add_camera", new=self.fake_add):
                await runner.start()
                runner.wake()
                for _ in range(100):
                    if self.job_state(job["id"]) == "done":
                        break
                    await asyncio.sleep(0.05)
                await runner.stop()

        asyncio.run(body())
        self.assertEqual(self.job_state(job["id"]), "done")

    def job_state(self, job_id: str) -> str:
        return self.rows("SELECT state FROM camera_add_jobs WHERE id=?", job_id)[0][0]


if __name__ == "__main__":
    unittest.main()
