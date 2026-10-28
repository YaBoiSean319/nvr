"""The multi-site console (#141): two real NVRs federated into a third, with access grants, an unreachable site, and tokens that only look and watch."""
from __future__ import annotations

import json
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import console
from app.store import connect
from tests.fixtures.nvr_site import NvrSite
from tests.test_access_grants import GrantCase

NOW = datetime.now(timezone.utc)


class ConsoleCase(GrantCase):
    @classmethod
    def setUpClass(cls):
        cls.a = NvrSite("alpha").start()
        cls.b = NvrSite("bravo").start()
        cls.a.seed(["Gate A1", "Gate A2"], group="Gates", rule="Gate entry")
        cls.b.seed(["Dock B1"], group="Docks", rule="Dock entry")
        cls.a_token = cls.a.token(["read", "media"])
        cls.b_token = cls.b.token(["read", "media"])

    @classmethod
    def tearDownClass(cls):
        cls.a.stop()
        cls.b.stop()

    def setUp(self):
        super().setUp()
        self.old = (console.registry, console.TIMEOUT)
        console.registry = console.Registry()
        console.TIMEOUT = httpx.Timeout(1.5, connect=1.0)
        self.old_inventory = console.INVENTORY_SECONDS
        console.INVENTORY_SECONDS = 0.0
        # The fixture sites are on this machine, which the address check refuses (as it should: see AddressTests); every other check is the real one.
        patch = mock.patch.object(console, "check_url", lambda url, allow_private: url.rstrip("/"))
        patch.start()
        self.addCleanup(patch.stop)
        self.site_ids: dict[str, str] = {}

    def tearDown(self):
        console.registry, console.TIMEOUT = self.old
        console.INVENTORY_SECONDS = self.old_inventory
        for site in (self.a, self.b):
            if site.process and site.process.poll() is None:
                site.resume()
        super().tearDown()

    def add(self, site: NvrSite, token: str, name: str | None = None):
        response = self.admin.post("/api/console/sites", json={"name": name or site.name.title(), "url": site.url, "token": token, "allow_private": True})
        if response.status_code == 201:
            self.site_ids[site.name] = response.json()["id"]
        return response

    def federate(self):
        self.assertEqual(self.add(self.a, self.a_token).status_code, 201)
        self.assertEqual(self.add(self.b, self.b_token).status_code, 201)
        self.assertEqual(self.admin.put("/api/console", json={"enabled": True}).status_code, 200)

    def cid(self, site: NvrSite, camera: str) -> str:
        return f"{self.site_ids[site.name]}~{site.made['cameras'][camera]}"

    def grant_site(self, user_id: int, *sites: NvrSite) -> None:
        response = self.admin.put("/api/admin/access-grants", json={"subject_type": "user", "subject": str(user_id),
                                                                    "grants": [{"scope_type": "site", "site_id": self.site_ids[site.name]} for site in sites]})
        self.assertEqual(response.status_code, 200, response.text)


class FederationTests(ConsoleCase):
    def test_cameras_from_both_sites_appear_grouped_by_site_with_nothing_about_how_they_are_reached(self):
        self.federate()
        data = self.admin.get("/api/cameras").json()
        by_name = {camera["name"]: camera for camera in data["cameras"]}
        self.assertEqual(set(by_name), {"Dock 1", "Dock 2", "Yard 1", "Loose", "Gate A1", "Gate A2", "Dock B1"})
        self.assertEqual(by_name["Gate A1"]["id"], self.cid(self.a, "Gate A1"))
        self.assertEqual((by_name["Gate A1"]["group_name"], by_name["Dock B1"]["group_name"]), ("Alpha", "Bravo"))
        remote = json.dumps([camera for camera in data["cameras"] if "site" in camera])
        for secret in ("10.77.", "secret-pass", "camera.invalid", "onvif/device_service"):
            self.assertNotIn(secret, remote)
        groups = {group["name"]: group for group in self.admin.get("/api/camera-groups").json()["groups"]}
        self.assertEqual(set(groups["Alpha"]["camera_ids"]), {self.cid(self.a, "Gate A1"), self.cid(self.a, "Gate A2")})
        self.assertEqual({site["name"]: site["state"] for site in data["sites"]}, {"Alpha": "ok", "Bravo": "ok"})

    def test_without_console_mode_nothing_changes(self):
        self.assertEqual(self.add(self.a, self.a_token).status_code, 201)
        data = self.admin.get("/api/cameras").json()
        self.assertNotIn("sites", data)
        self.assertEqual({camera["name"] for camera in data["cameras"]}, {"Dock 1", "Dock 2", "Yard 1", "Loose"})
        self.assertEqual(self.admin.get(f"/api/cameras/{self.cid(self.a, 'Gate A1')}/live/status").status_code, 404)

    def test_footage_and_pictures_come_through_the_site_with_ranges(self):
        self.federate()
        camera = self.cid(self.a, "Gate A1")
        segment = self.a.made["segment"]["id"]
        whole = self.admin.get(f"/api/cameras/{camera}/segments/{segment}/media")
        self.assertEqual(whole.status_code, 200)
        self.assertEqual(whole.content, bytes(range(256)) * 8)
        part = self.admin.get(f"/api/cameras/{camera}/segments/{segment}/media", headers={"Range": "bytes=10-19"})
        self.assertEqual((part.status_code, part.content), (206, (bytes(range(256)) * 8)[10:20]))
        self.assertEqual(self.admin.get(f"/api/cameras/{self.site_ids['alpha']}~no-such-camera/live/status").status_code, 404)

    def test_playback_timelines_merge_across_this_nvr_and_both_sites(self):
        self.federate()
        start, end = NOW - timedelta(hours=1), NOW + timedelta(minutes=5)
        cameras = [self.d1, self.cid(self.a, "Gate A1"), self.cid(self.b, "Dock B1"), self.cid(self.a, "Gate A2")]
        response = self.admin.post("/api/playback/timeline", json={"camera_ids": cameras, "start": start.isoformat(), "end": end.isoformat()})
        self.assertEqual(response.status_code, 200, response.text)
        entries = response.json()["cameras"]
        self.assertEqual([entry["camera_id"] for entry in entries], cameras)
        gate = entries[1]
        self.assertEqual(len(gate["segments"]), 1)
        self.assertEqual(gate["segments"][0]["media_url"], f"/api/cameras/{cameras[1]}/segments/{self.a.made['segment']['id']}/media")
        self.assertEqual(self.admin.get(gate["segments"][0]["media_url"]).status_code, 200)
        self.assertEqual(len(entries[2]["segments"]), 1)  # Dock B1 is Bravo's first camera, the one its fixture recorded
        self.assertEqual(entries[3]["segments"], [])
        unknown = self.admin.post("/api/playback/timeline", json={"camera_ids": [f"{self.site_ids['alpha']}~nobody"], "start": start.isoformat(), "end": end.isoformat()})
        self.assertEqual(unknown.status_code, 200)  # A camera the site does not know is the site's answer (an unreachable-style stub), not an error here

    def test_alarms_merge_with_counts_and_open_through_the_console_but_are_handled_at_the_site(self):
        self.federate()
        listed = self.admin.get("/api/alarms", params={"state": "all"}).json()
        names = {alarm["rule_name"]: alarm for alarm in listed["alarms"]}
        self.assertEqual(set(names), {"Gate entry", "Dock entry"})
        gate = names["Gate entry"]
        self.assertEqual((gate["id"], gate["camera_id"], gate["site"]["name"]), (f"{self.site_ids['alpha']}~{self.a.made['alarm']}", self.cid(self.a, "Gate A1"), "Alpha"))
        self.assertEqual(listed["counts"]["new"], 2)
        detail = self.admin.get(f"/api/alarms/{gate['id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["id"], gate["id"])
        self.assertTrue(detail.json()["snapshot_url"].startswith(f"/api/cameras/{self.cid(self.a, 'Gate A1')}/segments/"))
        self.assertEqual(self.admin.get(detail.json()["clip"]["segments"][0]["media_url"]).status_code, 200)
        refused = self.admin.post(f"/api/alarms/{gate['id']}/acknowledge")
        self.assertEqual(refused.status_code, 409)
        self.assertIn("Alpha", refused.json()["detail"])
        only = self.admin.get("/api/alarms", params={"state": "all", "camera_id": self.cid(self.b, "Dock B1")}).json()
        self.assertEqual({alarm["rule_name"] for alarm in only["alarms"]}, {"Dock entry"})  # Only that camera's, from its site
        self.assertEqual({alarm["rule_name"] for alarm in self.admin.get("/api/alarms", params={"state": "all", "camera_id": self.cid(self.a, "Gate A1")}).json()["alarms"]}, {"Gate entry"})

    def test_search_fans_out_and_thumbnails_come_through(self):
        self.federate()
        window = {"start": (NOW - timedelta(days=1)).isoformat(), "end": (NOW + timedelta(hours=1)).isoformat()}
        found = self.admin.get("/api/ai/search", params={**window, "upper_colour": "red"}).json()
        self.assertEqual({result["camera_id"] for result in found["results"]}, {self.cid(self.a, "Gate A1"), self.cid(self.b, "Dock B1")})
        result = next(item for item in found["results"] if item["site"]["name"] == "Alpha")
        self.assertEqual(result["track_id"], f"{self.site_ids['alpha']}~{self.a.made['track']}")
        self.assertEqual(result["site"]["name"], "Alpha")
        picture = self.admin.get(result["thumbnail_url"])
        self.assertEqual((picture.status_code, picture.headers["content-type"]), (200, "image/jpeg"))
        narrowed = self.admin.get("/api/ai/search", params={**window, "camera_ids": [self.cid(self.b, "Dock B1")]}).json()
        self.assertEqual({result["camera_id"] for result in narrowed["results"]}, {self.cid(self.b, "Dock B1")})

    def test_live_video_is_relayed_with_the_sites_own_messages(self):
        self.federate()
        # The site refuses a camera it does not have in its own words; the console relays them and the close code.
        with self.admin.websocket_connect(f"/api/cameras/{self.site_ids['alpha']}~no-such-camera/live/stream") as websocket:
            message = json.loads(websocket.receive_text())
            self.assertEqual(message["type"], "not_found")
            with self.assertRaises(WebSocketDisconnect) as closed:
                websocket.receive_text()
            self.assertEqual(closed.exception.code, 4404)


class GrantTests(ConsoleCase):
    def test_a_grant_limits_an_operator_to_one_sites_cameras(self):
        self.federate()
        self.grant_site(self.limited_id, self.a)
        names = {camera["name"] for camera in self.limited.get("/api/cameras").json()["cameras"]}
        self.assertEqual(names, {"Gate A1", "Gate A2"})
        self.assertEqual({group["name"] for group in self.limited.get("/api/camera-groups").json()["groups"]}, {"Alpha"})
        status = self.limited.get("/api/console/status").json()
        self.assertEqual([site["name"] for site in status["sites"]], ["Alpha"])
        self.assertEqual(self.limited.get(f"/api/cameras/{self.cid(self.a, 'Gate A1')}/live/status").status_code, 200)
        self.assertEqual(self.limited.get(f"/api/cameras/{self.cid(self.b, 'Dock B1')}/live/status").status_code, 404)  # As if it did not exist
        self.assertEqual({alarm["rule_name"] for alarm in self.limited.get("/api/alarms", params={"state": "all"}).json()["alarms"]}, {"Gate entry"})
        denied = self.limited.post("/api/playback/timeline", json={"camera_ids": [self.cid(self.b, "Dock B1")], "start": (NOW - timedelta(hours=1)).isoformat(), "end": NOW.isoformat()})
        self.assertEqual(denied.status_code, 422)
        found = self.limited.get("/api/ai/search", params={"start": (NOW - timedelta(days=1)).isoformat(), "end": (NOW + timedelta(hours=1)).isoformat()}).json()
        self.assertEqual({result["site"]["name"] for result in found["results"]}, {"Alpha"})
        self.assertEqual(self.limited.get(f"/api/alarms/{self.site_ids['bravo']}~{self.b.made['alarm']}").status_code, 404)
        with self.limited.websocket_connect(f"/api/cameras/{self.cid(self.b, 'Dock B1')}/live/stream") as websocket:
            self.assertEqual(json.loads(websocket.receive_text())["type"], "not_found")

    def test_other_grants_say_nothing_about_sites_and_an_all_grant_covers_every_site(self):
        self.federate()
        # `limited` has only the Dock group (a local one): no site. `full` has every camera: both sites.
        self.assertEqual({camera["name"] for camera in self.limited.get("/api/cameras").json()["cameras"]}, {"Dock 1", "Dock 2"})
        self.assertEqual(self.limited.get("/api/console/status").json()["sites"], [])
        self.assertEqual({camera["name"] for camera in self.full.get("/api/cameras").json()["cameras"]} >= {"Gate A1", "Dock B1"}, True)
        self.assertEqual({site["name"] for site in self.full.get("/api/console/status").json()["sites"]}, {"Alpha", "Bravo"})

    def test_site_grants_are_listed_replaced_and_expire(self):
        self.federate()
        self.grant_site(self.limited_id, self.a, self.b)
        listed = self.admin.get("/api/admin/access-grants").json()
        site_grants = [grant for grant in listed["grants"] if grant["scope_type"] == "site"]
        self.assertEqual({grant["site_name"] for grant in site_grants}, {"Alpha", "Bravo"})
        self.assertEqual({site["name"] for site in listed["sites"]}, {"Alpha", "Bravo"})
        self.grant_site(self.limited_id, self.b)
        self.assertEqual({site["name"] for site in self.limited.get("/api/console/status").json()["sites"]}, {"Bravo"})
        with connect() as db:
            db.execute("UPDATE site_grants SET expires_at=?", ((NOW - timedelta(seconds=1)).isoformat(),))
        self.assertEqual(self.limited.get("/api/console/status").json()["sites"], [])
        bad = self.admin.put("/api/admin/access-grants", json={"subject_type": "user", "subject": str(self.limited_id), "grants": [{"scope_type": "site", "site_id": "nosuchsite"}]})
        self.assertEqual(bad.status_code, 422)

    def test_removing_a_site_removes_its_grants(self):
        self.federate()
        self.grant_site(self.limited_id, self.a)
        self.assertEqual(self.admin.delete(f"/api/console/sites/{self.site_ids['alpha']}").status_code, 204)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM site_grants").fetchone()[0], 0)
        self.assertEqual(self.limited.get("/api/console/status").json()["sites"], [])


class UnreachableTests(ConsoleCase):
    def test_a_site_that_stops_answering_is_marked_unreachable_and_the_other_keeps_working(self):
        self.federate()
        self.assertEqual(len(self.admin.get("/api/cameras").json()["cameras"]), 7)
        self.b.pause()
        try:
            first = time.monotonic()
            data = self.admin.get("/api/alarms", params={"state": "all"}).json()  # The first request finds out (the site holds the connection and says nothing)
            self.assertGreaterEqual(time.monotonic() - first, 1.0)
            self.assertEqual({site["name"]: site["state"] for site in data["sites"]}, {"Alpha": "ok", "Bravo": "unreachable"})
            self.assertEqual({alarm["rule_name"] for alarm in data["alarms"]}, {"Gate entry"})
            # From then on the console does not wait for it: a merged answer takes about as long as asking Alpha alone.
            started = time.monotonic()
            for _ in range(3):
                listed = self.admin.get("/api/cameras").json()
            self.assertLess(time.monotonic() - started, 1.2)
            self.assertEqual({camera["name"]: camera["site_unreachable"] for camera in listed["cameras"] if "site_unreachable" in camera},
                             {"Gate A1": False, "Gate A2": False, "Dock B1": True})  # Bravo's cameras are last known, and said to be unreachable
            status = {site["name"]: site for site in self.admin.get("/api/console/status").json()["sites"]}
            self.assertEqual(status["Bravo"]["state"], "unreachable")
            self.assertIsNotNone(status["Bravo"]["last_contact_at"])
            self.assertEqual(status["Alpha"]["state"], "ok")
            timeline = self.admin.post("/api/playback/timeline", json={"camera_ids": [self.cid(self.a, "Gate A1"), self.cid(self.b, "Dock B1")],
                                                                      "start": (NOW - timedelta(hours=1)).isoformat(), "end": (NOW + timedelta(minutes=5)).isoformat()}).json()
            self.assertEqual(len(timeline["cameras"][0]["segments"]), 1)
            self.assertEqual(timeline["cameras"][1]["recording_status"], "site_unreachable")
            self.assertEqual(self.admin.get(f"/api/cameras/{self.cid(self.b, 'Dock B1')}/live/status").status_code, 502)
        finally:
            self.b.resume()
        # Once it answers again, it is tried again after the pause, and is well.
        self.assertEqual(self.admin.post(f"/api/console/sites/{self.site_ids['bravo']}/check").status_code, 200)
        time.sleep(0.2)
        recovered = self.admin.get("/api/cameras").json()
        self.assertEqual({site["name"]: site["state"] for site in recovered["sites"]}["Bravo"], "ok")

    def test_a_stopped_site_is_unreachable_at_once(self):
        site = NvrSite("charlie").start()
        try:
            site.seed(["Yard C1"], group="Yards")
            self.assertEqual(self.add(site, site.token(["read", "media"])).status_code, 201)
            self.admin.put("/api/console", json={"enabled": True})
            self.assertIn("Yard C1", {camera["name"] for camera in self.admin.get("/api/cameras").json()["cameras"]})
        finally:
            site.stop()
        started = time.monotonic()
        data = self.admin.get("/api/cameras").json()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual({site["name"]: site["state"] for site in data["sites"]}, {"Charlie": "unreachable"})
        self.assertTrue(all(camera["site_unreachable"] for camera in data["cameras"] if "site_unreachable" in camera))


class AddressTests(unittest.TestCase):
    def test_a_sites_address_is_checked_like_any_destination_the_server_is_told_to_call(self):
        for url in ("http://127.0.0.1:8280", "http://169.254.169.254/", "ftp://nvr.example.com", "http://user:pass@10.0.0.5/", "https://nvr.example.com/#x"):
            with self.assertRaises(console.ConsoleError, msg=url):
                console.check_url(url, True)
        with self.assertRaises(console.ConsoleError):
            console.check_url("http://10.0.0.5:8280", False)  # A private-network address needs the administrator to say so
        self.assertEqual(console.check_url("http://10.0.0.5:8280/", True), "http://10.0.0.5:8280")


class TokenTests(ConsoleCase):
    def test_a_token_with_more_power_than_the_console_needs_is_refused(self):
        admin_token = self.a.token(["admin"])
        refused = self.add(self.a, admin_token)
        self.assertEqual(refused.status_code, 422)
        self.assertIn("only the read and media scopes", refused.json()["detail"])

    def test_a_token_that_cannot_watch_video_is_refused(self):
        refused = self.add(self.a, self.a.token(["read"]))
        self.assertEqual(refused.status_code, 422)
        self.assertIn("media scope", refused.json()["detail"])

    def test_a_wrong_token_and_a_wrong_address_are_refused_plainly(self):
        self.assertEqual(self.add(self.a, "nvrt_0000000000000000_not-a-real-secret-at-all").status_code, 422)
        wrong = self.admin.post("/api/console/sites", json={"name": "Nowhere", "url": "http://127.0.0.1:9", "token": "x" * 30, "allow_private": True})
        self.assertEqual(wrong.status_code, 422)

    def test_the_token_is_stored_encrypted_and_never_returned(self):
        self.federate()
        with connect() as db:
            stored = [bytes(row[0]) for row in db.execute("SELECT token_ciphertext FROM console_sites")]
        self.assertEqual(len(stored), 2)
        for blob in stored:
            self.assertNotIn(self.a_token.encode(), blob)
            self.assertNotIn(self.b_token.encode(), blob)
        text = json.dumps([self.admin.get("/api/console").json(), self.admin.get("/api/console/status").json(), self.full.get("/api/console/status").json(),
                           self.admin.get("/api/admin/access-grants").json()])
        for secret in (self.a_token, self.b_token, self.a_token.split("_")[-1]):
            self.assertNotIn(secret, text)

    def test_the_console_cannot_change_anything_at_a_site(self):
        self.federate()
        camera = self.cid(self.a, "Gate A1")
        for method, path in (("PATCH", f"/api/cameras/{camera}"), ("POST", f"/api/cameras/{camera}/credentials"), ("DELETE", f"/api/cameras/{camera}/ptz/presets/1")):
            response = self.admin.request(method, path, json={"name": "renamed"})
            self.assertIn(response.status_code, (403, 404, 405, 422), (method, path, response.status_code))
        self.assertEqual({camera["name"] for camera in self.a.admin().get("/api/cameras").json()["cameras"]}, {"Gate A1", "Gate A2"})

    def test_the_proxy_only_reads_and_never_goes_outside_a_cameras_own_pages(self):
        self.federate()
        camera = self.cid(self.a, "Gate A1")
        segment = self.a.made["segment"]["id"]
        for path in (f"/api/cameras/{camera}/segments/{segment}/media/../../../../api/admin/sessions", f"/api/cameras/{camera}/%2e%2e/%2e%2e/admin/sessions",
                     f"/api/cameras/{camera}/credentials", f"/api/cameras/{camera}/segments/{segment}/media?%75nmasked=1&unmasked=true"):
            response = self.admin.get(path)
            self.assertIn(response.status_code, (200, 400, 404, 422), path)
            self.assertNotIn("sessions", response.text[:200] if response.status_code == 200 else "")
        self.assertEqual(self.admin.post(f"/api/cameras/{camera}/live/status", json={}).status_code in (404, 405), True)

    def test_a_site_that_answers_nonsense_is_left_out_without_harming_the_rest(self):
        self.federate()
        real = console.call

        async def garbled(link, method, path, **kwargs):
            if link.name == "Alpha" and path == "/api/alarms":
                return httpx.Response(200, json={"alarms": [{"nothing": "useful"}], "counts": {}, "rules": []})
            return await real(link, method, path, **kwargs)

        with mock.patch.object(console, "call", garbled):
            response = self.admin.get("/api/alarms?state=all&limit=50")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("nothing", response.text)

    def test_sites_are_for_administrators(self):
        self.federate()
        for client in (self.limited, self.full):
            self.assertEqual(client.get("/api/console").status_code, 403)
            self.assertEqual(client.put("/api/console", json={"enabled": False}).status_code, 403)
            self.assertEqual(client.post("/api/console/sites", json={"name": "x", "url": self.a.url, "token": self.a_token}).status_code, 403)
            self.assertEqual(client.delete(f"/api/console/sites/{self.site_ids['alpha']}").status_code, 403)
        self.assertEqual(TestClient(self.app).get("/api/console/status").status_code, 401)

    def test_a_site_can_be_renamed_switched_off_and_its_token_replaced_without_showing_it(self):
        self.federate()
        site = self.site_ids["alpha"]
        renamed = self.admin.put(f"/api/console/sites/{site}", json={"name": "Alpha North"})
        self.assertEqual((renamed.status_code, renamed.json()["name"]), (200, "Alpha North"))
        self.assertNotIn("token", json.dumps(renamed.json()))
        self.assertEqual(self.admin.put(f"/api/console/sites/{site}", json={"enabled": False}).json()["state"], "disabled")
        self.assertNotIn("Gate A1", {camera["name"] for camera in self.admin.get("/api/cameras").json()["cameras"]})
        self.assertEqual(self.admin.put(f"/api/console/sites/{site}", json={"enabled": True, "token": self.a.token(["read", "media"])}).status_code, 200)
        self.assertIn("Gate A1", {camera["name"] for camera in self.admin.get("/api/cameras").json()["cameras"]})
        self.assertEqual(self.admin.put(f"/api/console/sites/{site}", json={"token": self.a.token(["admin"])}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
