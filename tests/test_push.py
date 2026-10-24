from __future__ import annotations

import base64
import json
import struct
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from app import push
from app.principal import COOKIE_NAME
from app.rules import RulesManager
from app.store import connect
from tests.scene_case import SceneTestCase

BOTTOM_LEFT = {"x": 0.0, "y": 0.6, "width": 0.3, "height": 0.4}
# The real endpoint rule, before the tests let the local stand-in through.
REAL_VALIDATE_ENDPOINT = push.validate_endpoint


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Device:
    """A browser's push subscription keys, and decryption as the browser does it (RFC 8291)."""

    def __init__(self) -> None:
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.public = self.private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.auth = b"0123456789abcdef"

    def keys(self) -> dict[str, str]:
        return {"p256dh": b64(self.public), "auth": b64(self.auth)}

    def decrypt(self, body: bytes) -> dict:
        salt, record_size, key_length = body[:16], struct.unpack(">I", body[16:20])[0], body[20]
        sender = body[21:21 + key_length]
        ciphertext = body[21 + key_length:]
        assert record_size >= len(ciphertext)
        shared = self.private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender))
        ikm = HKDF(hashes.SHA256(), 32, salt=self.auth, info=b"WebPush: info\x00" + self.public + sender).derive(shared)
        key = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
        nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        assert plaintext.endswith(b"\x02")
        return json.loads(plaintext[:-1])


class PushService:
    """A local stand-in for a browser vendor's push service."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 201
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                service.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                self.send_response(service.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class EncryptionTests(unittest.TestCase):
    def test_payload_encryption_matches_the_rfc_8291_example(self):
        # RFC 8291 Appendix A: fixed sender key and salt give exactly the published message.
        sender = ec.derive_private_key(int.from_bytes(unb64("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
        body = push.encrypt_payload(
            b"When I grow up, I want to be a watermelon",
            "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4", "BTBZMqHH6r4Tts7J_aSIgg",
            sender=sender, salt=unb64("DGv6ra1nlYgDCS1FRnbzlw"),
        )
        self.assertEqual(b64(body), "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")

    def test_the_whole_message_fits_the_push_service_limit(self):
        device = Device()
        largest = push.MAX_PAYLOAD_BYTES
        self.assertEqual(len(push.encrypt_payload(b"x" * largest, **device.keys())), 4096)
        with self.assertRaises(push.PushError):
            push.encrypt_payload(b"x" * (largest + 1), **device.keys())


class PushTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        self.service = PushService()
        self.addCleanup(self.service.close)
        # The local stand-in is plain HTTP on loopback; real push services must be public HTTPS.
        allow_local = patch("app.push.validate_endpoint", side_effect=lambda endpoint: endpoint)
        allow_local.start()
        self.addCleanup(allow_local.stop)
        rule = self.client.post("/api/rules", json={
            "name": "Gate entry", "enabled": True, "camera_ids": [self.camera_id], "group_ids": [], "classes": ["vehicle"],
            "condition": {"type": "zone_entry", "zone": BOTTOM_LEFT}, "schedule": None, "cooldown_seconds": 0,
        })
        self.assertEqual(rule.status_code, 201, rule.text)
        self.rule_id = rule.json()["id"]
        self.device = Device()

    def subscribe(self, client=None, name="phone") -> str:
        endpoint = f"{self.service.origin}/push/{name}"
        response = (client or self.client).post("/api/push/subscriptions", json={"endpoint": endpoint, "keys": self.device.keys(), "device_label": name})
        self.assertEqual(response.status_code, 201, response.text)
        return endpoint

    def raise_alarm(self) -> str:
        self.analyse_footage(companions=[RulesManager(poll_seconds=0.05)])
        alarms = self.client.get("/api/alarms").json()["alarms"]
        self.assertEqual(len(alarms), 1)
        return alarms[0]["id"]

    def test_a_subscribed_device_receives_an_encrypted_signed_push_that_opens_the_alarm(self):
        self.subscribe()
        alarm_id = self.raise_alarm()
        self.record_scene_footage()
        self.assertEqual(push.deliver_due(), ["sent"])

        self.assertEqual(len(self.service.requests), 1)
        request = self.service.requests[0]
        self.assertEqual(request["path"], "/push/phone")
        self.assertEqual(request["headers"]["Content-Encoding"], "aes128gcm")
        self.assertEqual(request["headers"]["TTL"], "3600")
        message = self.device.decrypt(request["body"])
        self.assertEqual(message["alarm_id"], alarm_id)
        self.assertEqual(message["url"], f"/?alarm={alarm_id}")
        self.assertEqual(message["title"], f"Gate entry: {self.camera_name}")
        self.assertTrue(message["body"].startswith("Vehicle at "))

        # The VAPID signature verifies with the key browsers subscribe with, for this push service.
        public = self.client.get("/api/push/public-key").json()["public_key"]
        scheme, _, parameters = request["headers"]["Authorization"].partition(" ")
        fields = dict(part.strip().split("=", 1) for part in parameters.split(","))
        self.assertEqual((scheme, fields["k"]), ("vapid", public))
        header, claims, signature = fields["t"].split(".")
        verifier = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(public))
        raw = unb64(signature)
        verifier.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                        f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
        self.assertEqual(json.loads(unb64(claims))["aud"], self.service.origin)

        # The thumbnail opens without any session, through a link that names only this alarm.
        cookie = self.client.cookies.get(COOKIE_NAME)
        self.assertNotIn(cookie, message["thumbnail_url"])
        with TestClient(self.client.app) as device:
            thumbnail = device.get(message["thumbnail_url"])
        self.assertEqual(thumbnail.status_code, 200, thumbnail.text[:200])
        self.assertEqual(thumbnail.headers["content-type"], "image/jpeg")
        self.assertEqual(self.client.get("/api/push/preferences").json()["devices"][0]["device_label"], "phone")

    def test_a_fresh_alarm_has_a_thumbnail_before_its_recording_is_indexed(self):
        # Recorded segments are indexed when they close, up to a minute after the alarm; the
        # object's first analysed frame stands in until then.
        self.subscribe()
        alarm_id = self.raise_alarm()
        with TestClient(self.client.app) as device:
            thumbnail = device.get(f"/api/push/thumbnails/{push.thumbnail_token(alarm_id)}")
        self.assertEqual(thumbnail.status_code, 200, thumbnail.text[:200])
        self.assertEqual(thumbnail.headers["content-type"], "image/jpeg")
        self.assertTrue(thumbnail.content.startswith(b"\xff\xd8"))

    def test_thumbnail_links_expire_and_cannot_be_forged(self):
        self.subscribe()
        alarm_id = self.raise_alarm()
        self.record_scene_footage()
        token = push.thumbnail_token(alarm_id)
        with TestClient(self.client.app) as device:
            self.assertEqual(device.get(f"/api/push/thumbnails/{token}").status_code, 200)
            later = datetime.now(timezone.utc) + push.THUMBNAIL_LIFETIME + timedelta(seconds=1)
            with patch("app.push._now", return_value=later):
                self.assertEqual(device.get(f"/api/push/thumbnails/{token}").status_code, 410)
            other, expires, signature = token.rsplit(".", 2)
            self.assertEqual(device.get(f"/api/push/thumbnails/someone-else.{expires}.{signature}").status_code, 410)

    def test_quiet_hours_and_rule_choices_suppress_notifications(self):
        self.subscribe()
        now = datetime.now(timezone.utc)
        quiet = {"start": (now - timedelta(hours=1)).strftime("%H:%M"), "end": (now + timedelta(hours=1)).strftime("%H:%M"), "timezone": "UTC"}
        saved = self.client.put("/api/push/preferences", json={"rule_ids": None, "group_ids": None, "quiet_hours": quiet})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.raise_alarm()
        self.assertEqual(push.deliver_due(), [])

        # Outside quiet hours, a person who chose only other rules is still not notified.
        self.client.put("/api/push/preferences", json={"rule_ids": ["another-rule"], "group_ids": None, "quiet_hours": None})
        with connect() as db:
            alarm_id = db.execute("SELECT id FROM alarms").fetchone()[0]
            self.assertEqual(push.queue_for_alarm(db, alarm_id), 0)
        self.client.put("/api/push/preferences", json={"rule_ids": [self.rule_id], "group_ids": None, "quiet_hours": None})
        with connect() as db:
            self.assertEqual(push.queue_for_alarm(db, alarm_id), 1)
        self.assertEqual(push.deliver_due(), ["sent"])

    def test_gone_subscriptions_are_removed_and_failures_retry_with_backoff(self):
        self.subscribe()
        self.service.status = 500
        self.raise_alarm()
        self.assertEqual(push.deliver_due(), ["retrying"])
        with connect() as db:
            delivery = db.execute("SELECT status,attempt_count,next_attempt_at,last_error FROM push_deliveries").fetchone()
        self.assertEqual((delivery["status"], delivery["attempt_count"], delivery["last_error"]), ("queued", 1, "HTTP 500"))
        self.assertGreater(datetime.fromisoformat(delivery["next_attempt_at"]), datetime.now(timezone.utc))
        self.assertEqual(push.deliver_due(), [])  # not due yet

        self.service.status = 410
        with connect() as db:
            db.execute("UPDATE push_deliveries SET next_attempt_at=?", (datetime.now(timezone.utc).isoformat(),))
        self.assertEqual(push.deliver_due(), ["removed"])
        self.assertEqual(self.client.get("/api/push/preferences").json()["devices"], [])
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM push_deliveries").fetchone()[0], 0)

    def test_each_person_manages_only_their_own_devices(self):
        from app.security import hash_password
        from tests.test_support import TEST_PASSWORD

        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", ("watcher", hash_password(TEST_PASSWORD), "operator"))
        with TestClient(self.client.app) as operator:
            self.assertEqual(operator.post("/api/auth/login", json={"username": "watcher", "password": TEST_PASSWORD}).status_code, 200)
            endpoint = self.subscribe(operator, "watcher-phone")
            self.assertEqual([device["device_label"] for device in operator.get("/api/push/preferences").json()["devices"]], ["watcher-phone"])
            self.assertEqual(self.client.get("/api/push/preferences").json()["devices"], [])
            self.client.post("/api/push/subscriptions/remove", json={"endpoint": endpoint})
            self.assertEqual(len(operator.get("/api/push/preferences").json()["devices"]), 1)
            self.assertEqual(operator.post("/api/push/subscriptions/remove", json={"endpoint": endpoint}).status_code, 204)
            self.assertEqual(operator.get("/api/push/preferences").json()["devices"], [])

    def test_real_subscriptions_must_use_a_public_https_push_service(self):
        for endpoint in ("http://push.example.com/abc", "https://127.0.0.1/abc", "https://10.0.0.5/abc", "https://[::1]/abc"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                REAL_VALIDATE_ENDPOINT(endpoint)


if __name__ == "__main__":
    unittest.main()
