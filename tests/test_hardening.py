"""Camera network hardening checks (#124): fixture cameras with each weakness, secrets never kept, acknowledgements, and probe limits."""
from __future__ import annotations

import json
import logging
import socket
import sqlite3
import ssl
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import hardening
from app.credentials import encrypt
from app.store import connect
from tests.test_access_grants import GrantCase

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
NETWORK = "10.20.0.0/24"
SECRET_PASSWORD = "xc3511"  # A bundled default, distinctive enough to find anywhere it should not be.


class Offers:
    """A prober that says which cameras offer a secure alternative, and counts how often it was asked."""

    def __init__(self, offers: dict[tuple[str, str], int] | None = None, delay: float = 0.0) -> None:
        self.offers = offers or {}
        self.calls: list[tuple[str, str]] = []
        self.delay = delay
        self.active = self.peak = 0
        self.lock = threading.Lock()

    def __call__(self, host: str, kind: str) -> int | None:
        with self.lock:
            self.calls.append((host, kind))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            return self.offers.get((host, kind))
        finally:
            with self.lock:
                self.active -= 1


class HardeningCase(GrantCase):
    def setUp(self):
        super().setUp()
        with connect() as db:
            db.execute("DELETE FROM hardening_findings")
        # No test reaches a real address: the prober is the fixture's unless a test names its own.
        patcher = mock.patch("app.hardening.tls_offered", Offers())
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_camera(self, camera_id: str, stream: str | None = None, device: str | None = None, ip: str | None = None, onvif: dict | None = None, substream: str | None = None) -> None:
        with connect() as db:
            if stream is not None:
                db.execute("UPDATE cameras SET stream_url_ciphertext=? WHERE id=?", (encrypt(stream), camera_id))
            if substream is not None:
                db.execute("UPDATE cameras SET live_substream_url_ciphertext=? WHERE id=?", (encrypt(substream), camera_id))
            if device is not None:
                db.execute("UPDATE cameras SET device_service_url=? WHERE id=?", (device, camera_id))
            if ip is not None:
                db.execute("UPDATE cameras SET camera_ip=? WHERE id=?", (ip, camera_id))
            if onvif is not None:
                db.execute("UPDATE cameras SET motion_credentials_ciphertext=? WHERE id=?", (encrypt(json.dumps(onvif)), camera_id))

    def run_checks(self, offers: Offers | None = None, now: datetime = NOW, kind: str = "manual") -> dict:
        return hardening.run(kind, offers or Offers(), now)

    def findings_for(self, result: dict, camera_id: str, key: str = "findings") -> dict[str, dict]:
        return {item["check"]: item for item in result[key] if item["camera_id"] == camera_id}

    def make_clean(self, camera_id: str, host: str = "10.20.0.5") -> None:
        self.set_camera(camera_id, stream=f"rtsps://viewer:Zq9-long-unique-passphrase@{host}:322/main", device=f"https://{host}/onvif/device_service", ip=host,
                        onvif={"username": "viewer", "password": "Zq9-long-unique-passphrase"})


class NetworkTests(unittest.TestCase):
    def test_networks_are_normalised_and_validated(self):
        self.assertEqual(hardening.parse_networks(["10.20.0.7/24", " 10.20.0.0/24 ", "fd00::1/64"]), ["10.20.0.0/24", "fd00::/64"])
        for bad in (["not-a-network"], ["0.0.0.0/0"], ["10.0.0.0/8"] * 17, "10.0.0.0/8", [""]):
            with self.assertRaises(ValueError, msg=bad):
                hardening.parse_networks(bad)


class DefaultCredentialTests(HardeningCase):
    def test_a_default_login_in_the_stream_address_is_a_finding(self):
        self.set_camera(self.d1, stream=f"rtsp://root:{SECRET_PASSWORD}@10.20.0.5/main")
        finding = self.findings_for(self.run_checks(), self.d1)["default_credentials"]
        self.assertEqual((finding["severity"], finding["camera_name"]), ("high", "Dock 1"))
        self.assertIn("its stream login", finding["detail"])
        self.assertIn("Set a strong, unique password", finding["fix"])

    def test_a_default_onvif_login_and_both_together(self):
        self.set_camera(self.d1, onvif={"username": "Admin", "password": "admin"})  # The user is compared without regard to case.
        self.assertIn("its ONVIF login", self.findings_for(self.run_checks(), self.d1)["default_credentials"]["detail"])
        self.set_camera(self.d1, stream="rtsp://admin:12345@10.20.0.5/main")
        both = self.findings_for(self.run_checks(), self.d1)["default_credentials"]
        self.assertIn("its stream login and its ONVIF login", both["detail"])

    def test_a_blank_default_password_and_the_substream_count(self):
        self.set_camera(self.d1, stream="rtsp://admin:@10.20.0.5/main")
        self.assertIn("default_credentials", self.findings_for(self.run_checks(), self.d1))
        self.set_camera(self.d2, stream="rtsp://owner:Zq9-long-unique-passphrase@10.20.0.6/main", substream="rtsp://admin:admin@10.20.0.6/sub")
        self.assertIn("default_credentials", self.findings_for(self.run_checks(), self.d2))

    def test_other_logins_are_not_findings(self):
        # A default password for a user the list does not pair it with, a default user with a chosen password, and no login at all.
        self.set_camera(self.d1, stream="rtsp://alice:admin@10.20.0.5/main")
        self.set_camera(self.d2, stream="rtsp://admin:Zq9-long-unique-passphrase@10.20.0.6/main", onvif={"username": "admin", "password": "Zq9-long-unique-passphrase"})
        self.set_camera(self.y1, stream="rtsp://10.20.0.7/main")
        result = self.run_checks()
        for camera in (self.d1, self.d2, self.y1):
            self.assertNotIn("default_credentials", self.findings_for(result, camera))

    def test_the_tried_credentials_are_never_logged_or_stored(self):
        self.set_camera(self.d1, stream=f"rtsp://root:{SECRET_PASSWORD}@10.20.0.5/main", onvif={"username": "root", "password": SECRET_PASSWORD})
        captured: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record.getMessage())

        handler = Capture(level=logging.DEBUG)
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            result = self.admin.post("/api/hardening/run")
            self.assertEqual(result.status_code, 200, result.text)
            self.admin.get("/api/hardening")
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        with connect() as db:
            stored = "\n".join(f"{table}: {tuple(row)}" for table in ("hardening_findings", "hardening_probes", "hardening_settings", "audit_log")
                               for row in db.execute(f"SELECT * FROM {table}"))
        everything = "\n".join([stored, result.text, self.admin.get("/api/hardening").text, *captured])
        self.assertNotIn(SECRET_PASSWORD, everything)
        self.assertNotIn(f"root:{SECRET_PASSWORD}", everything)
        self.assertIn("default_credentials", stored)  # It did run, and found the weakness.


class ProtocolTests(HardeningCase):
    def test_plain_rtsp_where_rtsps_is_offered_is_a_finding(self):
        self.set_camera(self.d1, stream="rtsp://viewer:Zq9-long-unique-passphrase@10.20.0.5:554/main")
        finding = self.findings_for(self.run_checks(Offers({("10.20.0.5", "rtsps"): 322})), self.d1)["plain_rtsp"]
        self.assertEqual(finding["severity"], "medium")
        self.assertIn("rtsp://10.20.0.5:554", finding["detail"])
        self.assertIn("port 322", finding["detail"])
        self.assertIn("rtsps://", finding["fix"])

    def test_plain_rtsp_is_not_a_finding_when_nothing_secure_is_offered_or_the_stream_is_already_secure(self):
        self.set_camera(self.d1, stream="rtsp://viewer:Zq9-long-unique-passphrase@10.20.0.5/main")
        self.set_camera(self.d2, stream="rtsps://viewer:Zq9-long-unique-passphrase@10.20.0.6:322/main")
        offers = Offers({("10.20.0.6", "rtsps"): 322})
        result = self.run_checks(offers)
        self.assertNotIn("plain_rtsp", self.findings_for(result, self.d1))
        self.assertNotIn("plain_rtsp", self.findings_for(result, self.d2))
        self.assertNotIn(("10.20.0.6", "rtsps"), offers.calls)  # An RTSPS stream has nothing to look for.

    def test_onvif_over_http_where_https_is_offered_is_a_finding(self):
        self.set_camera(self.d1, device="http://10.20.0.5:80/onvif/device_service")
        self.set_camera(self.d2, device="http://10.20.0.6/onvif/device_service")
        result = self.run_checks(Offers({("10.20.0.5", "onvif_https"): 443}))
        finding = self.findings_for(result, self.d1)["onvif_http"]
        self.assertIn("http://10.20.0.5:80", finding["detail"])
        self.assertIn("HTTPS on port 443", finding["detail"])
        self.assertNotIn("onvif_http", self.findings_for(result, self.d2))

    def test_onvif_over_https_is_not_probed_or_flagged(self):
        self.set_camera(self.d1, device="https://10.20.0.5/onvif/device_service")
        offers = Offers({("10.20.0.5", "onvif_https"): 443})
        self.assertNotIn("onvif_http", self.findings_for(self.run_checks(offers), self.d1))
        self.assertNotIn(("10.20.0.5", "onvif_https"), offers.calls)


class NetworkCheckTests(HardeningCase):
    def configure(self, *networks: str) -> None:
        with connect() as db:
            hardening.set_networks(db, list(networks))

    def test_an_address_outside_the_camera_network_is_a_finding(self):
        self.configure(NETWORK)
        self.set_camera(self.d1, ip="192.168.1.5", stream="rtsp://10.20.0.5/main")
        self.set_camera(self.d2, ip="10.20.0.6", stream="rtsp://172.16.0.9/main", device="http://10.20.0.6/onvif")
        result = self.run_checks()
        first = self.findings_for(result, self.d1)["outside_network"]
        self.assertIn("192.168.1.5", first["detail"])
        second = self.findings_for(result, self.d2)["outside_network"]
        self.assertIn("172.16.0.9", second["detail"])
        self.assertNotIn("10.20.0.6,", second["detail"])

    def test_cameras_on_the_network_hostnames_and_unconfigured_networks_are_not_flagged(self):
        self.configure(NETWORK, "fd00::/64")
        self.set_camera(self.d1, ip="10.20.0.5", stream="rtsp://10.20.0.5/main")
        self.set_camera(self.d2, ip="fd00::7", stream="rtsp://[fd00::7]/main")
        # A name cannot be placed on a network from here, so it is not judged.
        self.set_camera(self.y1, stream="rtsp://camera.invalid/main")
        result = self.run_checks()
        for camera in (self.d1, self.d2, self.y1):
            self.assertNotIn("outside_network", self.findings_for(result, camera))
        self.configure()
        self.set_camera(self.d1, ip="192.168.1.5")
        result = self.run_checks()
        self.assertNotIn("outside_network", self.findings_for(result, self.d1))
        self.assertFalse(result["network_check"])

    def test_archived_cameras_have_no_findings(self):
        self.set_camera(self.d1, stream="rtsp://admin:admin@10.20.0.5/main")
        self.assertIn("default_credentials", self.findings_for(self.run_checks(), self.d1))
        with connect() as db:
            db.execute("UPDATE cameras SET archived_at=? WHERE id=?", (NOW.isoformat(), self.d1))
            self.assertEqual(hardening.report(db)["findings"], [])  # Not shown even before the next run.
        self.assertEqual(self.findings_for(self.run_checks(), self.d1), {})


class FixtureTests(HardeningCase):
    def test_cameras_with_each_weakness_produce_the_matching_findings_and_a_clean_one_none(self):
        self.configure_networks()
        self.make_clean(self.d1, "10.20.0.5")
        self.set_camera(self.d2, stream=f"rtsp://root:{SECRET_PASSWORD}@10.20.0.6/main", device="http://10.20.0.6/onvif", ip="10.20.0.6")
        self.set_camera(self.y1, stream="rtsp://owner:Zq9-long-unique-passphrase@192.168.9.9/main", ip="192.168.9.9")
        self.make_clean(self.loose, "10.20.0.8")
        result = self.run_checks(Offers({("10.20.0.6", "onvif_https"): 443}))
        self.assertEqual(self.findings_for(result, self.d1), {})
        self.assertEqual(self.findings_for(result, self.loose), {})
        self.assertEqual(set(self.findings_for(result, self.d2)), {"default_credentials", "onvif_http"})
        self.assertEqual(set(self.findings_for(result, self.y1)), {"outside_network"})
        self.assertEqual(result["counts"], {"high": 1, "medium": 2, "acknowledged": 0})
        self.assertEqual([item["severity"] for item in result["findings"]], ["high", "medium", "medium"])  # Worst first.

    def configure_networks(self) -> None:
        with connect() as db:
            hardening.set_networks(db, [NETWORK])

    def test_a_clean_fixture_produces_no_findings(self):
        self.configure_networks()
        for camera, host in ((self.d1, "10.20.0.5"), (self.d2, "10.20.0.6"), (self.y1, "10.20.0.7"), (self.loose, "10.20.0.8")):
            self.make_clean(camera, host)
        result = self.run_checks(Offers({(host, kind): 322 for host in ("10.20.0.5", "10.20.0.6") for kind in ("rtsps", "onvif_https")}))
        self.assertEqual((result["findings"], result["acknowledged"]), ([], []))
        self.assertTrue(result["network_check"])


class AcknowledgementTests(HardeningCase):
    def weak(self) -> None:
        self.set_camera(self.d1, stream="rtsp://admin:admin@10.20.0.5/main")

    def test_an_acknowledged_finding_is_hidden_with_its_reason_until_the_condition_changes(self):
        self.weak()
        self.run_checks()
        with connect() as db:
            self.assertTrue(hardening.acknowledge(db, self.d1, "default_credentials", "Lab camera, isolated", 1, NOW))
        result = self.run_checks()
        self.assertEqual(result["findings"], [])
        (hidden,) = result["acknowledged"]
        self.assertEqual((hidden["camera_id"], hidden["reason"], hidden["acknowledged_at"]), (self.d1, "Lab camera, isolated", NOW.isoformat()))
        self.assertEqual(self.run_checks(now=NOW + timedelta(days=30))["findings"], [])  # Time alone does not bring it back.
        # The condition changes: the ONVIF login is a default too. That is a different finding, shown again.
        self.set_camera(self.d1, onvif={"username": "admin", "password": "admin"})
        result = self.run_checks()
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual((result["acknowledged"], result["findings"][0]["reason"]), ([], None))

    def test_a_fixed_finding_that_returns_is_shown_again(self):
        self.weak()
        self.run_checks()
        with connect() as db:
            hardening.acknowledge(db, self.d1, "default_credentials", "Accepted", 1)
        self.set_camera(self.d1, stream="rtsp://admin:Zq9-long-unique-passphrase@10.20.0.5/main")
        self.assertEqual(self.run_checks()["acknowledged"], [])
        self.weak()
        self.assertEqual(len(self.run_checks()["findings"]), 1)

    def test_a_finding_can_be_restored(self):
        self.weak()
        self.run_checks()
        with connect() as db:
            hardening.acknowledge(db, self.d1, "default_credentials", "Accepted", 1)
            self.assertTrue(hardening.restore(db, self.d1, "default_credentials"))
            self.assertFalse(hardening.restore(db, self.d1, "plain_rtsp"))
        self.assertEqual(len(self.run_checks()["findings"]), 1)


class ProbeLimitTests(HardeningCase):
    def plain(self, camera: str, host: str) -> None:
        self.set_camera(camera, stream=f"rtsp://viewer:Zq9-long-unique-passphrase@{host}/main", device=f"http://{host}/onvif")

    def test_a_camera_is_probed_once_per_kind_within_the_probe_gap_and_again_after_it(self):
        self.plain(self.d1, "10.20.0.5")
        offers = Offers({("10.20.0.5", "rtsps"): 322})
        for minutes in (0, 5, 30, 59):
            result = self.run_checks(offers, NOW + timedelta(minutes=minutes))
        self.assertEqual(sorted(call for call in offers.calls if call[0] == "10.20.0.5"), [("10.20.0.5", "onvif_https"), ("10.20.0.5", "rtsps")])
        self.assertIn("plain_rtsp", self.findings_for(result, self.d1))  # The remembered answer still gives the finding.
        self.run_checks(offers, NOW + hardening.PROBE_GAP + timedelta(seconds=1))
        self.assertEqual(len([call for call in offers.calls if call[0] == "10.20.0.5"]), 4)

    def test_a_changed_address_is_probed_at_once_and_a_finding_clears_when_the_answer_changes(self):
        self.plain(self.d1, "10.20.0.5")
        offers = Offers({("10.20.0.5", "rtsps"): 322})
        self.assertIn("plain_rtsp", self.findings_for(self.run_checks(offers), self.d1))
        self.plain(self.d1, "10.20.0.6")
        self.assertNotIn("plain_rtsp", self.findings_for(self.run_checks(offers, NOW + timedelta(minutes=1)), self.d1))
        self.assertIn(("10.20.0.6", "rtsps"), offers.calls)

    def test_probes_never_run_more_than_the_limit_at_once(self):
        for index in range(10):
            camera = self.add_camera(f"Bulk {index}")
            self.plain(camera, f"10.20.1.{index + 1}")
        offers = Offers(delay=0.05)
        self.run_checks(offers)
        self.assertEqual(len([call for call in offers.calls if call[0].startswith("10.20.1.")]), 20)  # Ten addresses, each asked for both kinds once.
        self.assertLessEqual(offers.peak, hardening.PROBE_CONCURRENCY)
        self.assertGreater(offers.peak, 1)

    def test_a_probe_that_gets_no_answer_keeps_the_finding_and_its_acknowledgement_while_a_no_removes_them(self):
        self.plain(self.d1, "10.20.0.5")
        self.run_checks(Offers({("10.20.0.5", "rtsps"): 322}))
        with connect() as db:
            hardening.acknowledge(db, self.d1, "plain_rtsp", "Accepted", 1)

        def silent(host: str, kind: str) -> int | None:
            raise hardening.Unreachable(kind)
        result = hardening.run("manual", silent, NOW + hardening.PROBE_GAP + timedelta(seconds=1))
        self.assertEqual([item["check"] for item in result["acknowledged"]], ["plain_rtsp"])  # Busy or offline: nothing was learned, nothing changes.
        with connect() as db:
            self.assertEqual(db.execute("SELECT port FROM hardening_probes WHERE kind='rtsps'").fetchone()[0], 322)  # And the old answer is not overwritten.
        result = hardening.run("manual", Offers(), NOW + 2 * hardening.PROBE_GAP)  # It answered, and offers nothing: the finding goes.
        self.assertEqual((result["findings"], result["acknowledged"]), ([], []))

    def test_a_second_camera_at_an_address_just_probed_is_not_a_new_question(self):
        self.plain(self.d1, "10.20.0.5")
        offers = Offers({("10.20.0.5", "rtsps"): 322})
        self.run_checks(offers)
        self.plain(self.d2, "10.20.0.5")
        result = self.run_checks(offers, NOW + timedelta(minutes=5))
        self.assertEqual(len([call for call in offers.calls if call[0] == "10.20.0.5"]), 2)
        self.assertIn("plain_rtsp", self.findings_for(result, self.d2))

    def test_one_camera_with_an_odd_stored_address_does_not_stop_the_others(self):
        self.set_camera(self.d1, stream="rtsp://admin:admin@10.20.0.5:notaport/main", substream="rtsp://10.20.0.5:99999999/x")
        self.set_camera(self.d2, stream="rtsp://admin:admin@10.20.0.6/main")
        result = self.run_checks()
        self.assertIn("default_credentials", self.findings_for(result, self.d2))

    def test_a_probe_that_fails_to_run_is_no_finding_and_is_not_remembered(self):
        self.plain(self.d1, "10.20.0.5")
        def broken(host: str, kind: str) -> int | None:
            raise RuntimeError("boom")
        result = hardening.run("manual", broken, NOW)
        self.assertEqual(self.findings_for(result, self.d1), {})
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hardening_probes").fetchone()[0], 0)

    def test_the_api_run_uses_the_remembered_answers_too(self):
        self.plain(self.d1, "10.20.0.5")
        offers = Offers({("10.20.0.5", "rtsps"): 322})
        with mock.patch("app.hardening.tls_offered", offers):
            for _ in range(3):
                self.assertEqual(self.admin.post("/api/hardening/run").status_code, 200)
        self.assertEqual(len([call for call in offers.calls if call[0] == "10.20.0.5"]), 2)

    def test_cameras_at_one_address_are_probed_once(self):
        for camera in (self.d1, self.d2, self.y1):
            self.plain(camera, "10.20.0.5")
        offers = Offers({("10.20.0.5", "rtsps"): 322})
        result = self.run_checks(offers)
        self.assertEqual(sorted(call for call in offers.calls if call[0] == "10.20.0.5"), [("10.20.0.5", "onvif_https"), ("10.20.0.5", "rtsps")])
        for camera in (self.d1, self.d2, self.y1):
            self.assertIn("plain_rtsp", self.findings_for(result, camera))


class TlsProbeTests(unittest.TestCase):
    def serve(self, tls: bool):
        from datetime import timedelta as delta
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture")])
        now = datetime.now(timezone.utc)
        certificate = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1).not_valid_before(now - delta(days=1)) \
            .not_valid_after(now + delta(days=1)).sign(key, hashes.SHA256())
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cert_path, key_path = Path(directory.name, "cert.pem"), Path(directory.name, "key.pem")
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(0.2)
        stop = threading.Event()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)

        def loop() -> None:
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                except OSError:
                    continue
                try:
                    if tls:
                        context.wrap_socket(connection, server_side=True).close()
                    else:
                        connection.sendall(b"RTSP/1.0 400 Bad Request\r\n\r\n")
                        connection.close()
                except (OSError, ssl.SSLError):
                    connection.close()

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        self.addCleanup(lambda: (stop.set(), listener.close(), thread.join(2)))
        return listener.getsockname()[1]

    def test_a_self_signed_tls_listener_is_offered_and_a_plain_or_closed_port_is_not(self):
        secure, plain = self.serve(True), self.serve(False)
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        closed_port = closed.getsockname()[1]
        closed.close()
        with mock.patch.dict(hardening.PORTS, {"rtsps": (closed_port, secure), "onvif_https": (plain,), "closed": (closed_port,)}), mock.patch.object(hardening, "PROBE_TIMEOUT_SECONDS", 2.0):
            self.assertEqual(hardening.tls_offered("127.0.0.1", "rtsps"), secure)  # The closed port is passed over; the certificate need not be trusted.
            self.assertIsNone(hardening.tls_offered("127.0.0.1", "onvif_https"))  # A listener that does not speak TLS is not a secure alternative.
            self.assertIsNone(hardening.tls_offered("127.0.0.1", "closed"))  # A camera that refuses the connection has answered: it offers nothing there.

    def test_a_camera_that_does_not_answer_at_all_is_unknown_not_a_no(self):
        with mock.patch("socket.create_connection", side_effect=TimeoutError("timed out")), self.assertRaises(hardening.Unreachable):
            hardening.tls_offered("10.20.0.5", "rtsps")


class ScheduleTests(HardeningCase):
    def due(self, now: datetime) -> bool:
        with connect() as db:
            return hardening.due(db, now)

    def test_checks_run_weekly_counting_from_the_first_poll_and_never_at_startup(self):
        self.assertFalse(self.due(NOW))  # The first poll only starts the clock.
        self.assertFalse(self.due(NOW + timedelta(days=6, hours=23)))
        self.assertTrue(self.due(NOW + timedelta(days=7)))
        self.run_checks(now=NOW + timedelta(days=7), kind="scheduled")
        self.assertFalse(self.due(NOW + timedelta(days=13)))
        self.assertTrue(self.due(NOW + timedelta(days=14)))

    def test_the_manager_runs_a_due_week_as_a_scheduled_run_and_not_before(self):
        offers = Offers()
        with mock.patch("app.hardening.tls_offered", offers):
            hardening.HardeningManager.process_once()  # First poll: nothing is probed.
            self.assertEqual((offers.calls, self.admin.get("/api/hardening").json()["last_run_at"]), ([], None))
            with connect() as db:
                db.execute("UPDATE hardening_settings SET scheduled_from=?", ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),))
            self.set_camera(self.d1, stream="rtsp://viewer:Zq9-long-unique-passphrase@10.20.0.5/main")
            hardening.HardeningManager.process_once()
        report = self.admin.get("/api/hardening").json()
        self.assertEqual(report["last_run_kind"], "scheduled")
        self.assertIn(("10.20.0.5", "rtsps"), offers.calls)
        self.assertIsNotNone(report["next_run_at"])


class ApiTests(HardeningCase):
    def test_only_administrators_reach_the_report(self):
        for method, path, body in (("get", "/api/hardening", None), ("post", "/api/hardening/run", None), ("put", "/api/hardening/settings", {"camera_networks": []}),
                                   ("put", f"/api/hardening/findings/{self.d1}/default_credentials/acknowledgement", {"reason": "x"}),
                                   ("delete", f"/api/hardening/findings/{self.d1}/default_credentials/acknowledgement", None)):
            response = getattr(self.limited, method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual(response.status_code, 403, path)

    def test_camera_networks_are_validated_saved_audited_and_applied_at_once(self):
        self.set_camera(self.d1, ip="192.168.1.5", stream="rtsp://192.168.1.5/main")
        for bad in (["nonsense"], ["0.0.0.0/0"]):
            self.assertEqual(self.admin.put("/api/hardening/settings", json={"camera_networks": bad}).status_code, 422)
        response = self.admin.put("/api/hardening/settings", json={"camera_networks": ["10.20.0.9/24"]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["camera_networks"], body["network_check"]), ([NETWORK], True))
        self.assertIn(self.d1, {item["camera_id"] for item in body["findings"]})
        (entry,) = self.audit("hardening_networks_set")
        self.assertEqual(entry["details"], {"before": [], "after": [NETWORK]})
        self.assertEqual(self.admin.get("/api/hardening").json()["camera_networks"], [NETWORK])

    def test_probes_can_be_switched_off_and_the_protocol_checks_are_then_not_made(self):
        self.set_camera(self.d1, stream="rtsp://viewer:Zq9-long-unique-passphrase@10.20.0.5/main")
        offers = Offers({("10.20.0.5", "rtsps"): 322})
        with mock.patch("app.hardening.tls_offered", offers):
            self.assertIn("plain_rtsp", self.findings_for(self.admin.post("/api/hardening/run").json(), self.d1))
            calls = len(offers.calls)
            response = self.admin.put("/api/hardening/settings", json={"camera_networks": [], "probes_enabled": False})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["probes_enabled"])
            self.assertNotIn("plain_rtsp", self.findings_for(response.json(), self.d1))
            self.assertEqual(len(offers.calls), calls)  # Nothing was asked of a camera.
            self.assertEqual(self.audit("hardening_probes_set")[0]["details"], {"enabled": False})
            self.assertFalse(self.admin.put("/api/hardening/settings", json={"camera_networks": []}).json()["probes_enabled"])  # Left out, it is left as it is.
            self.assertTrue(self.admin.put("/api/hardening/settings", json={"camera_networks": [], "probes_enabled": True}).json()["probes_enabled"])

    def test_acknowledging_needs_a_reason_and_an_existing_finding_and_is_audited(self):
        self.set_camera(self.d1, stream="rtsp://admin:admin@10.20.0.5/main")
        self.assertEqual(self.admin.post("/api/hardening/run").status_code, 200)
        path = f"/api/hardening/findings/{self.d1}/default_credentials/acknowledgement"
        for body in ({}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 301}):
            self.assertEqual(self.admin.put(path, json=body).status_code, 422, body)
        self.assertEqual(self.admin.put(f"/api/hardening/findings/{self.d1}/plain_rtsp/acknowledgement", json={"reason": "x"}).status_code, 404)
        self.assertEqual(self.admin.put(f"/api/hardening/findings/{self.d1}/nonsense/acknowledgement", json={"reason": "x"}).status_code, 404)
        response = self.admin.put(path, json={"reason": "  Bench unit, no network route  "})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["findings"], body["counts"]["acknowledged"]), ([], 1))
        self.assertEqual((body["acknowledged"][0]["reason"], body["acknowledged"][0]["acknowledged_by"]), ("Bench unit, no network route", "keeper"))
        (entry,) = self.audit("hardening_finding_acknowledged")
        self.assertEqual((entry["resource_id"], entry["details"]), (self.d1, {"check": "default_credentials", "reason": "Bench unit, no network route"}))
        restored = self.admin.delete(path)
        self.assertEqual((restored.status_code, len(restored.json()["findings"])), (200, 1))
        self.assertEqual(len(self.audit("hardening_finding_restored")), 1)
        self.assertEqual(self.admin.delete(path).status_code, 404)

    def test_running_is_audited_and_reports_what_it_found(self):
        self.set_camera(self.d1, stream="rtsp://admin:admin@10.20.0.5/main")
        response = self.admin.post("/api/hardening/run")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["counts"]["high"], 1)
        self.assertEqual(self.audit("hardening_run")[0]["details"], {"high": 1, "medium": 0})
        self.assertEqual(response.json()["last_run_kind"], "manual")

    def test_removing_a_camera_removes_its_findings(self):
        self.set_camera(self.d1, stream="rtsp://admin:admin@10.20.0.5/main")
        self.run_checks()
        with connect() as db:
            db.execute("DELETE FROM cameras WHERE id=?", (self.d1,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hardening_findings WHERE camera_id=?", (self.d1,)).fetchone()[0], 0)


class LifecycleTests(unittest.TestCase):
    def test_every_manager_that_is_started_is_also_stopped(self):
        from app.application import SHUTDOWN_ORDER, STARTUP_ORDER
        self.assertEqual({name for name, _ in STARTUP_ORDER} - set(SHUTDOWN_ORDER), set())
        self.assertIn("hardening", {name for name, _ in STARTUP_ORDER})


class UpgradeTests(unittest.TestCase):
    def test_the_migration_creates_the_tables_and_a_settings_row(self):
        from app.migrations import m0023_hardening
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE cameras (id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        m0023_hardening.upgrade(db)
        self.assertEqual(db.execute("SELECT camera_networks_json FROM hardening_settings").fetchone()[0], "[]")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO hardening_settings(id) VALUES(2)")
        db.execute("INSERT INTO cameras(id) VALUES('c')")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO hardening_findings(camera_id,check_name,severity,fingerprint,detail,first_seen_at,last_seen_at) VALUES('c','nonsense','high','f','d','t','t')")


if __name__ == "__main__":
    unittest.main()
