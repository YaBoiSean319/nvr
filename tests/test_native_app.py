"""The native mobile app (#140): push through FCM and APNs on the web-push subscription model, device sessions, and nothing cached on the phone."""
from __future__ import annotations

import base64
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from app import device_sessions, native_push, push
from app.store import connect
from tests.test_access_grants import GrantCase

FCM_TOKEN = "fcm-registration-token:" + "A" * 60
APNS_TOKEN = "ab" * 32
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())
RSA_PEM = RSA_KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
EC_PEM = EC_KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
SERVICE_ACCOUNT = json.dumps({"type": "service_account", "project_id": "nvr-demo", "client_email": "push@nvr-demo.iam.gserviceaccount.com", "private_key": RSA_PEM})


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def parts(token: str) -> tuple[dict, dict, bytes, bytes]:
    head, body, signature = token.split(".")
    return json.loads(unb64(head)), json.loads(unb64(body)), unb64(signature), f"{head}.{body}".encode()


class Services:
    """Stands in for Google's and Apple's push services and records what the NVR sends them."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fcm_status, self.apns_status, self.apns_reason = 200, 200, ""
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
        if request.url.host == "fcm.googleapis.com":
            if self.fcm_status == 200:
                return httpx.Response(200, json={"name": "projects/nvr-demo/messages/1"})
            return httpx.Response(self.fcm_status, json=self.fcm_error if self.fcm_status == 404 else {"error": {"message": "The registration token is not a valid FCM registration token"}} if self.fcm_status == 400 else {"error": {"status": "PERMISSION_DENIED"}})
        return httpx.Response(self.apns_status, json={"reason": self.apns_reason} if self.apns_reason else {})

    fcm_error = {"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}}

    def to(self, host: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.host == host]


class NativeCase(GrantCase):
    def setUp(self):
        super().setUp()
        native_push.forget_tokens()
        self.services = Services()

    def configure(self, fcm=True, apns=True):
        body = {}
        if fcm:
            body["fcm_service_account"] = SERVICE_ACCOUNT
        if apns:
            body.update({"apns_key": EC_PEM, "apns_key_id": "ABC123DEFG", "apns_team_id": "TEAM123456", "apns_bundle_id": "app.nvr.mobile"})
        response = self.admin.put("/api/native/push-settings", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def register(self, client=None, platform="android", token=FCM_TOKEN, label="Pixel"):
        return (client or self.admin).post("/api/native/devices", json={"platform": platform, "token": token, "device_label": label})

    def deliver(self) -> list[str]:
        return push.deliver_due(self.services.client)


class DeviceTests(NativeCase):
    def test_a_device_registers_removes_and_changes_hands(self):
        self.assertEqual(self.register(self.limited).status_code, 201)
        self.assertEqual(self.register(self.limited, "ios", APNS_TOKEN.upper(), "iPhone").status_code, 201)
        with connect() as db:
            self.assertEqual(sorted((row["kind"], row["user_id"]) for row in db.execute("SELECT kind,user_id FROM push_subscriptions")), [("apns", self.limited_id), ("fcm", self.limited_id)])
        self.assertEqual(self.register(self.full).status_code, 201)  # The phone changes hands
        with connect() as db:
            self.assertEqual(db.execute("SELECT user_id FROM push_subscriptions WHERE kind='fcm'").fetchone()[0], self.full_id)
        self.assertEqual(self.limited.post("/api/native/devices/remove", json={"token": FCM_TOKEN}).status_code, 204)  # Not theirs any more
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE kind='fcm'").fetchone()[0], 1)
        self.assertEqual(self.full.post("/api/native/devices/remove", json={"token": FCM_TOKEN}).status_code, 204)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE kind='fcm'").fetchone()[0], 0)

    def test_tokens_that_are_not_the_push_services_are_refused(self):
        self.assertEqual(self.register(token="short").status_code, 422)
        self.assertEqual(self.register(platform="ios", token="not-hex-" + "z" * 60).status_code, 422)
        self.assertEqual(self.register(platform="android", token="bad token with spaces " * 3).status_code, 422)
        self.assertEqual(self.admin.post("/api/native/devices", json={"platform": "windows", "token": FCM_TOKEN}).status_code, 422)

    def test_devices_need_a_signed_in_person(self):
        self.assertEqual(self.register(TestClient(self.app)).status_code, 401)


class CredentialTests(NativeCase):
    def test_credentials_are_administrator_only_and_never_returned(self):
        view = self.configure()
        self.assertEqual((view["fcm_configured"], view["fcm_project_id"], view["apns_configured"], view["apns_bundle_id"]), (True, "nvr-demo", True, "app.nvr.mobile"))
        text = json.dumps(self.admin.get("/api/native/push-settings").json())
        for secret in ("PRIVATE KEY", "BEGIN", "client_email", "push@nvr-demo", "ABC123DEFG" + "x"):
            self.assertNotIn(secret, text)
        for client in (self.limited, self.full):
            self.assertEqual(client.get("/api/native/push-settings").status_code, 403)
            self.assertEqual(client.put("/api/native/push-settings", json={"fcm_service_account": SERVICE_ACCOUNT}).status_code, 403)
        with connect() as db:
            blob = db.execute("SELECT fcm_credentials_ciphertext,apns_key_ciphertext FROM native_push_settings").fetchone()
            self.assertNotIn(b"PRIVATE KEY", bytes(blob[0]) + bytes(blob[1]))

    def test_credentials_are_checked_and_can_be_removed(self):
        for body in ({"fcm_service_account": "{not json"}, {"fcm_service_account": json.dumps({"project_id": "x"})}, {"apns_key": "not a key", "apns_key_id": "ABC123DEFG", "apns_team_id": "TEAM123456", "apns_bundle_id": "app.nvr.mobile"},
                     {"apns_key": EC_PEM}, {"apns_key": EC_PEM, "apns_key_id": "short", "apns_team_id": "TEAM123456", "apns_bundle_id": "app.nvr.mobile"}):
            self.assertEqual(self.admin.put("/api/native/push-settings", json=body).status_code, 422, body)
        rsa_as_apns = {"apns_key": RSA_PEM, "apns_key_id": "ABC123DEFG", "apns_team_id": "TEAM123456", "apns_bundle_id": "app.nvr.mobile"}
        self.assertEqual(self.admin.put("/api/native/push-settings", json=rsa_as_apns).status_code, 422)
        self.configure()
        self.assertFalse(self.admin.put("/api/native/push-settings", json={"fcm_service_account": ""}).json()["fcm_configured"])
        self.assertTrue(self.admin.get("/api/native/push-settings").json()["apns_configured"])


class DeliveryTests(NativeCase):
    def setUp(self):
        super().setUp()
        self.configure()

    def test_an_alarm_reaches_an_android_phone_through_fcm_and_opens_it(self):
        self.register(self.limited)
        alarm = self.add_alarm(self.d1)
        self.assertEqual(self.deliver(), ["sent"])
        [exchange] = self.services.to("oauth2.googleapis.com")
        assertion = parse_qs(exchange.content.decode())["assertion"][0]
        head, claims, signature, signed = parts(assertion)
        self.assertEqual((head["alg"], claims["iss"], claims["aud"]), ("RS256", "push@nvr-demo.iam.gserviceaccount.com", native_push.FCM_TOKEN_URL))
        RSA_KEY.public_key().verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        [sent] = self.services.to("fcm.googleapis.com")
        self.assertTrue(str(sent.url).endswith("/v1/projects/nvr-demo/messages:send"))
        self.assertEqual(sent.headers["authorization"], "Bearer ya29.test")
        message = json.loads(sent.content)["message"]
        self.assertEqual(message["token"], FCM_TOKEN)
        self.assertEqual(message["data"]["alarm_id"], alarm)
        self.assertEqual(message["data"]["url"], f"/?alarm={alarm}")
        self.assertTrue(message["data"]["thumbnail_url"].startswith("/api/push/thumbnails/"))
        self.assertIn("Dock 1", message["notification"]["title"])
        self.assertEqual(message["android"]["priority"], "HIGH")

    def test_an_alarm_reaches_an_iphone_through_apns_signed_with_the_key(self):
        self.register(self.limited, "ios", APNS_TOKEN, "iPhone")
        alarm = self.add_alarm(self.d1)
        self.assertEqual(self.deliver(), ["sent"])
        [sent] = self.services.to("api.push.apple.com")
        self.assertEqual(sent.url.path, f"/3/device/{APNS_TOKEN}")
        self.assertEqual((sent.headers["apns-topic"], sent.headers["apns-push-type"], sent.headers["apns-priority"]), ("app.nvr.mobile", "alert", "10"))
        head, claims, signature, signed = parts(sent.headers["authorization"].split(" ", 1)[1])
        self.assertEqual((head["alg"], head["kid"], claims["iss"]), ("ES256", "ABC123DEFG", "TEAM123456"))
        EC_KEY.public_key().verify(encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")), signed, ec.ECDSA(hashes.SHA256()))
        body = json.loads(sent.content)
        self.assertEqual((body["alarm_id"], body["url"]), (alarm, f"/?alarm={alarm}"))
        self.assertIn("Dock 1", body["aps"]["alert"]["title"])

    def test_the_sandbox_is_used_when_the_key_is_not_for_production(self):
        self.admin.put("/api/native/push-settings", json={"apns_production": False})
        self.register(self.limited, "ios", APNS_TOKEN)
        self.add_alarm(self.d1)
        self.deliver()
        self.assertEqual(len(self.services.to("api.sandbox.push.apple.com")), 1)

    def test_people_and_rules_decide_who_is_told_as_for_web_push(self):
        self.register(self.limited, "android", FCM_TOKEN)
        self.register(self.full, "ios", APNS_TOKEN)
        self.add_alarm(self.y1)  # Only the person who may see the Yard camera
        self.assertEqual(self.deliver(), ["sent"])
        self.assertEqual((len(self.services.to("fcm.googleapis.com")), len(self.services.to("api.push.apple.com"))), (0, 1))
        self.assertEqual(self.limited.put("/api/push/preferences", json={"rule_ids": []}).status_code, 200)
        self.add_alarm(self.d1)
        self.assertEqual(self.deliver(), ["sent"])  # The person who chose no rules is not told; the other is
        self.assertEqual(len(self.services.to("fcm.googleapis.com")), 0)

    def test_a_device_the_service_has_forgotten_is_removed_but_rejected_credentials_do_not_remove_it(self):
        self.register(self.limited)
        self.services.fcm_status = 404
        self.add_alarm(self.d1)
        self.assertEqual(self.deliver(), ["removed"])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE kind='fcm'").fetchone()[0], 0)
        self.register(self.limited)
        self.services.fcm_status = 403
        self.add_alarm(self.d2)
        self.assertEqual(self.deliver(), ["retrying"])  # The credentials are the NVR's to fix; the phone stays registered
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE kind='fcm'").fetchone()[0], 1)
            self.assertIn("rejected", db.execute("SELECT last_error FROM push_deliveries WHERE status='queued'").fetchone()[0])

    def test_a_wrong_firebase_project_does_not_wipe_the_registrations(self):
        self.register(self.limited)
        self.services.fcm_status, self.services.fcm_error = 404, {"error": {"status": "NOT_FOUND", "message": "Requested entity was not found."}}
        self.add_alarm(self.d1)
        self.assertEqual(self.deliver(), ["retrying"])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE kind='fcm'").fetchone()[0], 1)

    def test_apns_forgets_only_unregistered_devices_and_server_errors_retry(self):
        self.register(self.limited, "ios", APNS_TOKEN)
        self.services.apns_status, self.services.apns_reason = 400, "BadDeviceToken"  # As likely the wrong environment as a stale token
        self.add_alarm(self.d1)
        self.assertEqual(self.deliver(), ["failed"])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE kind='apns'").fetchone()[0], 1)
        self.services.apns_status, self.services.apns_reason = 410, "Unregistered"
        self.add_alarm(self.d2)
        self.assertEqual(self.deliver(), ["removed"])
        self.register(self.limited, "ios", APNS_TOKEN)
        self.services.apns_status, self.services.apns_reason = 503, "ServiceUnavailable"
        self.add_alarm(self.d2)
        self.assertEqual(self.deliver(), ["retrying"])

    def test_without_credentials_the_delivery_fails_with_a_reason_instead_of_retrying_forever(self):
        self.admin.put("/api/native/push-settings", json={"fcm_service_account": ""})
        self.register(self.limited)
        self.add_alarm(self.d1)
        self.assertEqual(self.deliver(), ["failed"])
        with connect() as db:
            self.assertIn("not set up", db.execute("SELECT last_error FROM push_deliveries").fetchone()[0])

    def test_the_access_check_happens_again_at_send_time(self):
        self.register(self.limited)
        self.add_alarm(self.d1)
        with connect() as db:
            db.execute("DELETE FROM access_grants WHERE subject=?", (str(self.limited_id),))
        self.assertEqual(self.deliver(), ["failed"])
        self.assertEqual(self.services.requests, [])

    def test_web_subscriptions_are_unaffected(self):
        with connect() as db:
            self.assertEqual(db.execute("SELECT DISTINCT kind FROM push_subscriptions").fetchall(), [])


class DeviceSessionTests(NativeCase):
    def test_the_secret_signs_the_person_back_in_once_and_is_replaced(self):
        created = self.limited.post("/api/native/device-sessions", json={"device_label": "Pixel"})
        self.assertEqual(created.status_code, 201)
        first = created.json()["token"]
        self.assertTrue(first.startswith("nvrd_"))
        phone = TestClient(self.app)
        self.assertEqual(phone.get("/api/auth/me").status_code, 401)
        resumed = phone.post("/api/native/device-sessions/resume", json={"token": first})
        self.assertEqual((resumed.status_code, resumed.json()["username"], resumed.json()["role"]), (200, "limited", "operator"))
        self.assertEqual(phone.get("/api/auth/me").status_code, 200)
        second = resumed.json()["token"]
        self.assertNotEqual(first, second)
        # The secret it replaced still works for a couple of minutes (the phone may never have heard the answer), and then is useless.
        self.assertEqual(TestClient(self.app).post("/api/native/device-sessions/resume", json={"token": first}).status_code, 200)
        with connect() as db:
            db.execute("UPDATE device_sessions SET previous_until=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        self.assertEqual(TestClient(self.app).post("/api/native/device-sessions/resume", json={"token": first}).status_code, 401)

    def test_secrets_are_stored_only_as_digests_and_expire_and_can_be_revoked(self):
        token = self.limited.post("/api/native/device-sessions", json={}).json()["token"]
        with connect() as db:
            stored = db.execute("SELECT token_hash,id FROM device_sessions").fetchone()
            self.assertNotIn(token, stored["token_hash"])
            db.execute("UPDATE device_sessions SET expires_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        self.assertEqual(TestClient(self.app).post("/api/native/device-sessions/resume", json={"token": token}).status_code, 401)
        fresh = self.limited.post("/api/native/device-sessions", json={"device_label": "Spare"}).json()
        listed = self.limited.get("/api/native/device-sessions").json()["sessions"]
        self.assertEqual([item["device_label"] for item in listed], ["Spare"])
        self.assertNotIn("token", json.dumps(listed))
        self.assertEqual(self.full.delete(f"/api/native/device-sessions/{fresh['id']}").status_code, 404)  # Someone else's
        self.assertEqual(self.limited.delete(f"/api/native/device-sessions/{fresh['id']}").status_code, 204)
        self.assertEqual(TestClient(self.app).post("/api/native/device-sessions/resume", json={"token": fresh["token"]}).status_code, 401)

    def test_a_phone_signs_itself_out_by_its_secret_and_only_its_own(self):
        mine = self.limited.post("/api/native/device-sessions", json={"device_label": "Mine"}).json()["token"]
        theirs = self.full.post("/api/native/device-sessions", json={"device_label": "Theirs"}).json()["token"]
        self.assertEqual(self.limited.post("/api/native/device-sessions/revoke", json={"token": theirs}).status_code, 204)
        self.assertEqual(TestClient(self.app).post("/api/native/device-sessions/resume", json={"token": theirs}).status_code, 200)  # Not another person's
        self.assertEqual(self.limited.post("/api/native/device-sessions/revoke", json={"token": mine}).status_code, 204)
        self.assertEqual(TestClient(self.app).post("/api/native/device-sessions/resume", json={"token": mine}).status_code, 401)

    def test_anything_that_is_not_a_live_secret_is_the_same_401(self):
        for token in ("nvrd_" + "x" * 43, "x" * 50, "nvrd_", "y" * 30, "nvrd_" + "é" * 40):
            self.assertEqual(TestClient(self.app).post("/api/native/device-sessions/resume", json={"token": token}).status_code, 401)
        self.assertEqual(TestClient(self.app).post("/api/native/device-sessions", json={}).status_code, 401)

    def test_ending_a_persons_sessions_ends_their_phones_too(self):
        token = self.limited.post("/api/native/device-sessions", json={}).json()["token"]
        self.assertEqual(self.admin.delete(f"/api/admin/users/{self.limited_id}/mfa").status_code, 204)
        self.assertEqual(TestClient(self.app).post("/api/native/device-sessions/resume", json={"token": token}).status_code, 401)

    def test_a_removed_account_loses_its_phones_and_only_the_newest_ten_are_kept(self):
        for index in range(12):
            self.limited.post("/api/native/device-sessions", json={"device_label": f"phone {index}"})
        labels = [item["device_label"] for item in self.limited.get("/api/native/device-sessions").json()["sessions"]]
        self.assertEqual(len(labels), device_sessions.MAX_PER_USER)
        self.assertIn("phone 11", labels)
        self.assertNotIn("phone 0", labels)
        with connect() as db:
            db.execute("DELETE FROM users WHERE id=?", (self.limited_id,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM device_sessions").fetchone()[0], 0)


class DeviceSecretReplayTests(NativeCase):
    """A rotated-out secret is good for one short grace period, fixed when the replacement is first used (#293)."""

    def setUp(self):
        super().setUp()
        self.clock = [datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)]
        patcher = patch("app.device_sessions._now", lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def at(self, seconds: float) -> None:
        self.clock[0] = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)

    def resume(self, secret: str) -> httpx.Response:
        return TestClient(self.app).post("/api/native/device-sessions/resume", json={"token": secret})

    def enrol(self) -> str:
        return self.limited.post("/api/native/device-sessions", json={"device_label": "Pixel"}).json()["token"]

    def test_ordinary_rotation_keeps_working(self):
        first = self.enrol()
        second = self.resume(first).json()["token"]
        self.at(10)
        third = self.resume(second).json()["token"]
        self.at(20)
        self.assertEqual(self.resume(third).status_code, 200)

    def test_replaying_the_old_secret_does_not_keep_it_alive(self):
        # The reproduction: the same original secret resumed at 90, 180, 270 and 360 seconds after it was rotated (each reply restarted the window).
        original = self.enrol()
        self.assertEqual(self.resume(original).status_code, 200)  # rotated at t=0; the grace ends at t=120
        self.at(90)
        self.assertEqual(self.resume(original).status_code, 200, "a lost answer is retried inside the grace")
        for seconds in (100, 180, 270, 360):
            self.at(seconds)
            self.assertEqual(self.resume(original).status_code, 401, f"still accepted {seconds} s after the rotation")

    def test_the_old_secret_works_once_not_repeatedly(self):
        original = self.enrol()
        self.resume(original)
        self.at(30)
        self.assertEqual(self.resume(original).status_code, 200)
        self.at(31)
        self.assertEqual(self.resume(original).status_code, 401, "inside the grace, but already used for its one retry")

    def test_the_old_secret_is_refused_after_its_deadline_even_if_not_replayed_before(self):
        original = self.enrol()
        self.resume(original)
        self.at(121)
        self.assertEqual(self.resume(original).status_code, 401)

    def test_each_rotation_sets_its_own_deadline(self):
        first = self.enrol()
        second = self.resume(first).json()["token"]      # first is good until t=120
        self.at(100)
        third = self.resume(second).json()["token"]      # second is good until t=220; first is not extended by this
        self.at(110)
        self.assertEqual(self.resume(first).status_code, 401, "first was already replaced twice over")
        self.at(150)
        self.assertEqual(self.resume(second).status_code, 200, "second is inside the grace its own rotation set")
        self.assertEqual(self.resume(third).status_code, 401, "...and using it for the retry replaced the current secret once")

    def test_two_retries_at_once_let_exactly_one_through(self):
        original = self.enrol()
        self.resume(original)
        self.at(30)
        barrier, results = threading.Barrier(2), []

        def retry():
            barrier.wait()
            results.append(device_sessions.resume(original))

        threads = [threading.Thread(target=retry) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(result is not None for result in results), [False, True])


class DeviceEnrolmentBoundaryTests(NativeCase):
    """Only a person who is signed in can enrol a phone; automation never turns into a long-lived person credential (#292)."""

    def token(self, *scopes: str) -> str:
        created = self.admin.post("/api/admin/api-tokens", json={"name": "Automation", "scopes": list(scopes)})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()["secret"]

    def enrolled(self) -> int:
        with connect() as db:
            return db.execute("SELECT COUNT(*) FROM device_sessions").fetchone()[0]

    def test_an_admin_token_cannot_enrol_a_phone(self):
        secret = self.token("admin")
        with TestClient(self.app) as bare:  # No cookie: only the token authenticates
            denied = bare.post("/api/native/device-sessions", json={"device_label": "Sneaky"}, headers={"Authorization": f"Bearer {secret}"})
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(self.enrolled(), 0)

    def test_a_bearer_token_beside_a_valid_browser_cookie_is_still_a_token(self):
        secret = self.token("admin")
        # The same client holds the administrator's session cookie; the Authorization header still decides who is calling.
        denied = self.admin.post("/api/native/device-sessions", json={"device_label": "Sneaky"}, headers={"Authorization": f"Bearer {secret}"})
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(self.enrolled(), 0)

    def test_a_video_wall_cannot_enrol_a_phone(self):
        secret = self.token("read", "media")
        wall = TestClient(self.app)
        self.assertEqual(wall.post("/api/wall/session", json={"token": secret}).status_code, 200)
        denied = wall.post("/api/native/device-sessions", json={"device_label": "Wall"})
        self.assertIn(denied.status_code, (401, 403), denied.text)
        self.assertEqual(self.enrolled(), 0)

    def test_the_token_cannot_reach_session_only_management_by_any_exchange(self):
        # What the exchange used to give: a cookie that outlived the token and could manage tokens. The first step is now refused.
        secret = self.token("admin")
        with TestClient(self.app) as bare:
            bearer = {"Authorization": f"Bearer {secret}"}
            self.assertEqual(bare.get("/api/admin/api-tokens", headers=bearer).status_code, 403)
            self.assertEqual(bare.post("/api/native/device-sessions", json={}, headers=bearer).status_code, 403)
            self.assertEqual(bare.cookies.get("nvr_session"), None)

    def test_a_signed_in_person_still_enrols_a_phone(self):
        created = self.limited.post("/api/native/device-sessions", json={"device_label": "Pixel"})
        self.assertEqual(created.status_code, 201, created.text)
        admin_phone = self.admin.post("/api/native/device-sessions", json={"device_label": "Admin phone"})
        self.assertEqual(admin_phone.status_code, 201, admin_phone.text)
        self.assertEqual(self.enrolled(), 2)


class UpgradeRevokesOldDeviceSecretsTests(NativeCase):
    def test_every_existing_device_secret_is_ended_and_audited(self):
        from app.migrations import m0060_revoke_device_sessions as migration

        mine = self.limited.post("/api/native/device-sessions", json={"device_label": "Mine"}).json()
        theirs = self.full.post("/api/native/device-sessions", json={"device_label": "Theirs"}).json()
        with connect() as db:
            migration.upgrade(db)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM device_sessions").fetchone()[0], 0)
            audited = {row["resource_id"]: json.loads(row["details_json"]) for row in db.execute("SELECT * FROM audit_log WHERE action='device_session_revoked_by_upgrade'")}
        self.assertEqual(set(audited), {mine["id"], theirs["id"]})
        self.assertEqual(audited[mine["id"]]["device"], "Mine")
        for secret in (mine["token"], theirs["token"]):
            self.assertEqual(TestClient(self.app).post("/api/native/device-sessions/resume", json={"token": secret}).status_code, 401)
        # Signing in on the phone makes a new one.
        self.assertEqual(self.limited.post("/api/native/device-sessions", json={"device_label": "Mine"}).status_code, 201)


class RevokedPhoneTests(NativeCase):
    """Taking a phone away ends the browser sessions it produced, not only its ability to make more (#294)."""

    def phone(self, owner=None, label: str = "Pixel") -> tuple[TestClient, dict]:
        """A phone that has enrolled and resumed: its own client holding the cookie that came out of the resume."""
        enrolled = (owner or self.limited).post("/api/native/device-sessions", json={"device_label": label}).json()
        phone = TestClient(self.app)
        resumed = phone.post("/api/native/device-sessions/resume", json={"token": enrolled["token"]})
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(phone.get("/api/auth/me").status_code, 200)
        return phone, {**enrolled, "current": resumed.json()["token"]}

    def test_revoking_by_id_ends_the_phones_session(self):
        phone, device = self.phone()
        self.assertEqual(self.limited.delete(f"/api/native/device-sessions/{device['id']}").status_code, 204)
        self.assertEqual(phone.get("/api/auth/me").status_code, 401, "the cookie the phone was given must not outlive the phone")
        self.assertEqual(self.limited.get("/api/auth/me").status_code, 200, "the person's own browser session is untouched")

    def test_the_phone_signing_itself_out_by_its_secret_ends_its_session_too(self):
        phone, device = self.phone()
        other, _ = self.phone(label="Spare")
        self.assertEqual(self.limited.post("/api/native/device-sessions/revoke", json={"token": device["current"]}).status_code, 204)
        self.assertEqual(phone.get("/api/auth/me").status_code, 401)
        self.assertEqual(other.get("/api/auth/me").status_code, 200, "an unrelated phone of the same person stays signed in")

    def test_every_session_a_phone_has_been_given_ends_together(self):
        enrolled = self.limited.post("/api/native/device-sessions", json={"device_label": "Pixel"}).json()
        first, second = TestClient(self.app), TestClient(self.app)
        rotated = first.post("/api/native/device-sessions/resume", json={"token": enrolled["token"]}).json()["token"]
        self.assertEqual(second.post("/api/native/device-sessions/resume", json={"token": rotated}).status_code, 200)
        self.assertEqual((first.get("/api/auth/me").status_code, second.get("/api/auth/me").status_code), (200, 200))
        self.assertEqual(self.limited.delete(f"/api/native/device-sessions/{enrolled['id']}").status_code, 204)
        self.assertEqual((first.get("/api/auth/me").status_code, second.get("/api/auth/me").status_code), (401, 401))

    def test_one_persons_revocation_cannot_reach_another_persons_phone(self):
        theirs, device = self.phone(owner=self.full, label="Theirs")
        self.assertEqual(self.limited.delete(f"/api/native/device-sessions/{device['id']}").status_code, 404)
        self.assertEqual(self.limited.post("/api/native/device-sessions/revoke", json={"token": device["current"]}).status_code, 204)  # always 204, says nothing
        self.assertEqual(theirs.get("/api/auth/me").status_code, 200)

    def test_a_phone_that_is_pushed_out_by_newer_ones_loses_its_session(self):
        old, _ = self.phone(label="Oldest")
        for index in range(device_sessions.MAX_PER_USER):
            self.limited.post("/api/native/device-sessions", json={"device_label": f"newer {index}"})
        labels = [item["device_label"] for item in self.limited.get("/api/native/device-sessions").json()["sessions"]]
        self.assertNotIn("Oldest", labels)
        self.assertEqual(old.get("/api/auth/me").status_code, 401)

    def test_resuming_and_revoking_at_the_same_time_never_leaves_a_working_session(self):
        for _ in range(6):
            enrolled = self.limited.post("/api/native/device-sessions", json={"device_label": "Race"}).json()
            phone, barrier, answers = TestClient(self.app), threading.Barrier(2), {}

            def resume():
                barrier.wait()
                answers["resume"] = phone.post("/api/native/device-sessions/resume", json={"token": enrolled["token"]}).status_code

            def revoke():
                barrier.wait()
                answers["revoke"] = self.limited.delete(f"/api/native/device-sessions/{enrolled['id']}").status_code

            threads = [threading.Thread(target=resume), threading.Thread(target=revoke)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertIn(answers["resume"], (200, 401), answers)
            self.assertEqual(answers["revoke"], 204, answers)
            self.assertEqual(phone.get("/api/auth/me").status_code, 401, f"a session survived the revocation: {answers}")

    def test_a_revocation_is_audited_with_how_many_sessions_it_ended(self):
        _, device = self.phone(label="Audited")
        self.limited.delete(f"/api/native/device-sessions/{device['id']}")
        with connect() as db:
            row = db.execute("SELECT * FROM audit_log WHERE action='device_session_revoked' AND resource_id=?", (device["id"],)).fetchone()
        self.assertIsNotNone(row)
        details = json.loads(row["details_json"])
        self.assertEqual((details["device"], details["sessions_ended"]), ("Audited", 1))
        self.assertNotIn(device["token"], row["details_json"])

    def test_sessions_open_before_the_upgrade_have_no_device_and_are_left_alone(self):
        with connect() as db:
            self.assertIsNone(db.execute("SELECT device_session_id FROM sessions LIMIT 1").fetchone()["device_session_id"])
        self.phone(label="Unrelated")
        self.assertEqual(self.limited.get("/api/auth/me").status_code, 200)


class NothingCachedTests(NativeCase):
    def test_everything_the_app_fetches_is_marked_not_to_be_cached(self):
        native = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) NVRApp/1.0"}
        for path in ("/", "/api/auth/me", "/api/cameras"):
            response = self.admin.get(path, headers=native)
            self.assertEqual(response.headers.get("cache-control"), "no-store", path)
        browser = self.admin.get("/", headers={"User-Agent": "Mozilla/5.0 Firefox"})
        self.assertNotEqual(browser.headers.get("cache-control"), "no-store")


if __name__ == "__main__":
    unittest.main()
