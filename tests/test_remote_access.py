"""Secure remote access (#132): forwarded headers are believed only from trusted proxies, the live WebSocket checks origins behind a proxy,
Settings says how the NVR is reached and what is unsafe about it, and remote sessions can be made to use a second factor."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
import uvicorn
from starlette.requests import Request
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from app import network
from app.api.remote_access import warnings
from app.application import create_app
from app.security import hash_password
from app.store import connect as database
from tests.fixtures.reverse_proxy import ReverseProxy
from tests.test_support import TEST_PASSWORD

OPERATOR_PASSWORD = "operator-password-1234"
ENVIRONMENT = ("NVR_TRUSTED_PROXIES", "NVR_TRUST_FORWARDED_FOR", "NVR_LOCAL_NETWORKS", "NVR_ALLOWED_ORIGINS", "NVR_PUBLIC_URL", "NVR_COOKIE_SECURE")


def make_request(peer: str | None, headers: dict[str, str] | list[tuple[str, str]] | None = None, scheme: str = "http", host: str = "nvr.lan:8280") -> Request:
    pairs = list((headers or {}).items()) if isinstance(headers, dict) else list(headers or [])
    pairs = [("host", host), *pairs]
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "scheme": scheme, "server": ("nvr.lan", 8280),
                    "client": (peer, 40000) if peer else None, "headers": [(key.lower().encode(), value.encode()) for key, value in pairs]})


class NetworkCase(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.dict(os.environ, {}, clear=False)
        self.patch.start()
        for name in ENVIRONMENT:
            os.environ.pop(name, None)

    def tearDown(self):
        self.patch.stop()

    def trust(self, value: str) -> None:
        os.environ["NVR_TRUSTED_PROXIES"] = value


class ClientAddressTests(NetworkCase):
    def test_without_trusted_proxies_forwarded_headers_are_never_believed(self):
        request = make_request("198.51.100.4", {"x-forwarded-for": "6.6.6.6", "x-forwarded-proto": "https", "x-forwarded-host": "evil.example"})
        self.assertEqual(network.client_address(request), "198.51.100.4")
        self.assertEqual((network.scheme(request), network.host(request)), ("http", "nvr.lan:8280"))

    def test_only_a_trusted_peer_is_believed_and_the_address_is_the_first_entry_from_the_right_that_is_not_a_proxy(self):
        self.trust("10.0.0.0/8, 172.18.0.5")
        spoofed = {"x-forwarded-for": "6.6.6.6, 203.0.113.9, 10.1.1.1"}  # The caller prepended 6.6.6.6; the proxies added the rest
        self.assertEqual(network.client_address(make_request("10.0.0.7", spoofed)), "203.0.113.9")
        self.assertEqual(network.client_address(make_request("172.18.0.5", {"x-forwarded-for": "6.6.6.6, 203.0.113.9"})), "203.0.113.9")
        # The same header from anybody else is only a header.
        self.assertEqual(network.client_address(make_request("198.51.100.4", spoofed)), "198.51.100.4")
        self.assertEqual(network.client_address(make_request("10.0.0.7", {"x-forwarded-for": "2001:db8::7"})), "2001:db8::7")

    def test_a_chain_that_cannot_be_trusted_falls_back_to_the_peer(self):
        self.trust("10.0.0.0/8")
        for header in ("not-an-address, 10.1.1.1", "203.0.113.9, junk", "", ", ,", ", ".join(f"203.0.113.{index}" for index in range(11))):
            self.assertEqual(network.client_address(make_request("10.0.0.7", {"x-forwarded-for": header})), "10.0.0.7", header)
        self.assertEqual(network.client_address(make_request("10.0.0.7")), "10.0.0.7")
        self.assertEqual(network.client_address(make_request("10.0.0.7", {"x-forwarded-for": "10.2.2.2, 10.3.3.3"})), "10.2.2.2")  # All proxies: the first

    def test_a_header_sent_on_several_lines_counts_as_one_chain(self):
        self.trust("10.0.0.0/8")
        request = make_request("10.0.0.7", [("x-forwarded-for", "6.6.6.6"), ("x-forwarded-for", "203.0.113.9")])
        self.assertEqual(network.client_address(request), "203.0.113.9")

    def test_no_peer_at_all_is_unknown(self):
        self.assertEqual(network.client_address(make_request(None)), "unknown")

    def test_the_older_option_still_trusts_the_last_entry_of_anybody_and_says_so_in_settings(self):
        os.environ["NVR_TRUST_FORWARDED_FOR"] = "1"
        self.assertEqual(network.client_address(make_request("198.51.100.4", {"x-forwarded-for": "6.6.6.6, 203.0.113.9"})), "203.0.113.9")
        self.assertEqual(network.client_address(make_request("198.51.100.4")), "198.51.100.4")
        self.trust("198.51.100.0/24")  # Trusted proxies, once listed, replace it
        self.assertEqual(network.client_address(make_request("203.0.113.50", {"x-forwarded-for": "6.6.6.6"})), "203.0.113.50")

    def test_trusted_proxy_entries_that_are_not_addresses_are_reported_and_ignored(self):
        self.trust("10.0.0.0/8, nginx.internal, 300.1.1.1, 2001:db8::/32")
        networks, bad = network.parse_networks(os.environ["NVR_TRUSTED_PROXIES"])
        self.assertEqual(([str(item) for item in networks], bad), (["10.0.0.0/8", "2001:db8::/32"], ["nginx.internal", "300.1.1.1"]))
        self.assertEqual(network.client_address(make_request("10.0.0.7", {"x-forwarded-for": "203.0.113.9"})), "203.0.113.9")


class SchemeHostOriginTests(NetworkCase):
    def test_https_and_the_host_come_from_a_trusted_proxy_only(self):
        headers = {"x-forwarded-proto": "https", "x-forwarded-host": "nvr.example.test"}
        self.trust("10.0.0.0/8")
        trusted, other = make_request("10.0.0.7", headers), make_request("198.51.100.4", headers)
        self.assertEqual((network.scheme(trusted), network.host(trusted), network.origin(trusted)), ("https", "nvr.example.test", "https://nvr.example.test"))
        self.assertEqual((network.scheme(other), network.host(other)), ("http", "nvr.lan:8280"))
        self.assertEqual(network.scheme(make_request("198.51.100.4", scheme="https")), "https")
        self.assertEqual(network.scheme(make_request("198.51.100.4", scheme="wss")), "https")

    def test_a_forwarded_host_that_is_not_a_host_name_is_ignored_and_the_last_of_several_wins(self):
        self.trust("10.0.0.0/8")
        for value in ("evil.example/path", "a b", "x@y", "http://evil", "evil.example:99999999"):
            self.assertEqual(network.host(make_request("10.0.0.7", {"x-forwarded-host": value})), "nvr.lan:8280", value)
        self.assertEqual(network.host(make_request("10.0.0.7", {"x-forwarded-host": "first.example, nvr.example.test:8443"})), "nvr.example.test:8443")
        self.assertEqual(network.host(make_request("10.0.0.7", {"x-forwarded-host": "[::1]:8280"})), "[::1]:8280")
        self.assertEqual(network.scheme(make_request("10.0.0.7", {"x-forwarded-proto": "gopher"})), "http")
        self.assertEqual(network.scheme(make_request("10.0.0.7", {"x-forwarded-proto": "http, https"})), "https")

    def test_an_origin_is_allowed_when_it_names_the_host_asked_for_or_one_that_was_listed(self):
        request = make_request("198.51.100.4")
        self.assertTrue(network.origin_allowed(request, "http://nvr.lan:8280"))
        self.assertTrue(network.origin_allowed(request, "https://NVR.lan:8280"))  # The scheme does not matter, the host does
        for foreign in ("https://evil.example", "http://nvr.lan:9999", "null", "", "nvr.lan:8280", "javascript://nvr.lan:8280"):
            self.assertFalse(network.origin_allowed(request, foreign), foreign)
        os.environ["NVR_PUBLIC_URL"] = "https://nvr.example.test/"
        os.environ["NVR_ALLOWED_ORIGINS"] = "https://alias.example.test, http://10.1.1.1:8280/, not a url"
        self.assertTrue(network.origin_allowed(request, "https://nvr.example.test"))
        self.assertTrue(network.origin_allowed(request, "https://alias.example.test"))
        self.assertTrue(network.origin_allowed(request, "http://10.1.1.1:8280"))
        self.assertFalse(network.origin_allowed(request, "https://other.example.test"))
        self.assertEqual(network.allowed_origins(), ["http://10.1.1.1:8280", "https://alias.example.test", "https://nvr.example.test"])

    def test_behind_a_trusted_proxy_the_origin_is_the_host_the_proxy_reports_not_the_one_it_rewrote(self):
        self.trust("10.0.0.0/8")
        behind = make_request("10.0.0.7", {"x-forwarded-host": "nvr.example.test"}, host="127.0.0.1:8280")
        self.assertTrue(network.origin_allowed(behind, "https://nvr.example.test"))
        self.assertFalse(network.origin_allowed(behind, "https://evil.example"))
        unknown = make_request("198.51.100.4", {"x-forwarded-host": "nvr.example.test"}, host="127.0.0.1:8280")
        self.assertFalse(network.origin_allowed(unknown, "https://nvr.example.test"))


class RemoteTests(NetworkCase):
    def test_which_addresses_are_outside_the_local_network(self):
        for local in ("127.0.0.1", "::1", "10.2.3.4", "172.16.0.9", "172.31.255.1", "192.168.1.20", "169.254.1.1", "fe80::1", "fd12:3456::1", "100.64.0.1", "100.127.1.1"):
            self.assertFalse(network.is_remote(local), local)
        for remote in ("8.8.8.8", "203.0.113.9", "172.32.0.1", "100.128.0.1", "2001:db8::1", "unknown", "junk", ""):
            self.assertTrue(network.is_remote(remote), remote)
        os.environ["NVR_LOCAL_NETWORKS"] = "203.0.113.0/24, nonsense"
        self.assertFalse(network.is_remote("203.0.113.9"))

    def test_a_caller_behind_a_proxy_that_was_not_listed_counts_as_remote_not_local(self):
        local_through_unknown_proxy = make_request("192.168.1.10", {"x-forwarded-for": "203.0.113.9"})
        self.assertTrue(network.request_is_remote(local_through_unknown_proxy))
        self.assertFalse(network.request_is_remote(make_request("192.168.1.10")))
        self.trust("192.168.1.10")
        self.assertFalse(network.request_is_remote(make_request("192.168.1.10", {"x-forwarded-for": "192.168.1.55"})))
        self.assertTrue(network.request_is_remote(make_request("192.168.1.10", {"x-forwarded-for": "203.0.113.9"})))
        os.environ.pop("NVR_TRUSTED_PROXIES")
        os.environ["NVR_TRUST_FORWARDED_FOR"] = "1"  # The older option did choose to believe the header
        self.assertFalse(network.request_is_remote(make_request("192.168.1.10", {"x-forwarded-for": "192.168.1.55"})))


class WarningTests(NetworkCase):
    def info(self, **changes) -> dict:
        base = {"client_address": "203.0.113.9", "peer_address": "127.0.0.1", "peer_is_trusted_proxy": True, "remote": True, "https": True, "host": "nvr.example.test",
                "forwarded": {"for": "203.0.113.9", "proto": "https", "host": "nvr.example.test"}, "trusted_proxies": ["127.0.0.1/32"], "invalid_trusted_proxies": [],
                "legacy_forwarded_trust": False, "local_networks": [], "allowed_origins": [], "public_url": "https://nvr.example.test", "cookie_secure": True}
        return {**base, **changes}

    def codes(self, info: dict, policy: str = "all", remote_mfa: bool = False, role: str = "administrator") -> list[str]:
        return [item["code"] for item in warnings(info, policy, remote_mfa, role)]

    def test_a_well_set_up_remote_deployment_has_no_warnings(self):
        self.assertEqual(self.codes(self.info()), [])
        self.assertEqual(self.codes(self.info(), policy="off", remote_mfa=True), [])

    def test_plain_http_from_outside_the_local_network_is_the_worst_warning(self):
        found = warnings(self.info(https=False, cookie_secure=False), "off", False, "administrator")
        self.assertEqual([item["code"] for item in found], ["plain_http_remote", "no_second_factor"])
        self.assertEqual(found[0]["severity"], "bad")
        self.assertIn("203.0.113.9", found[0]["message"])
        self.assertNotIn("plain_http_remote", self.codes(self.info(https=False, remote=False, client_address="192.168.1.5", trusted_proxies=[], public_url=None)))

    def test_a_second_factor_is_expected_wherever_the_nvr_is_exposed(self):
        self.assertEqual(self.codes(self.info(), policy="off"), ["no_second_factor"])
        self.assertEqual(self.codes(self.info(), policy="administrators", role="operator"), ["no_second_factor"])
        self.assertEqual(self.codes(self.info(), policy="administrators", role="administrator"), [])
        quiet = self.info(remote=False, trusted_proxies=[], public_url=None, client_address="192.168.1.5", https=False)
        self.assertEqual(self.codes(quiet, policy="off"), [])  # Only reached locally: nothing is exposed that we can see

    def test_forwarded_headers_from_a_peer_that_is_not_a_trusted_proxy_are_called_out(self):
        info = self.info(peer_is_trusted_proxy=False, peer_address="172.18.0.5", trusted_proxies=[])
        found = warnings(info, "all", False, "administrator")
        self.assertIn("forwarded_headers_ignored", [item["code"] for item in found])
        self.assertIn("172.18.0.5", next(item["message"] for item in found if item["code"] == "forwarded_headers_ignored"))
        self.assertNotIn("forwarded_headers_ignored", self.codes(self.info(forwarded={"for": None, "proto": None, "host": None})))

    def test_the_older_trust_option_and_bad_entries_and_insecure_cookies_are_called_out(self):
        self.assertIn("legacy_forwarded_trust", self.codes(self.info(legacy_forwarded_trust=True, trusted_proxies=[])))
        self.assertNotIn("legacy_forwarded_trust", self.codes(self.info(legacy_forwarded_trust=True)))
        self.assertIn("invalid_trusted_proxies", self.codes(self.info(invalid_trusted_proxies=["nginx"])))
        self.assertIn("cookie_not_secure", self.codes(self.info(cookie_secure=False)))
        self.assertIn("public_url_plain_http", self.codes(self.info(public_url="http://nvr.example.test", remote=False, client_address="192.168.1.5")))


class LiveServer:
    """The real application under uvicorn on a loopback port, with its forwarded-header handling left to the NVR (as in production)."""

    def __init__(self, app) -> None:
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", proxy_headers=False, lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("The server did not start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(15)


class ProxiedCase(NetworkCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        for key, value in {"NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_RECORDINGS_PATH": str(root / "recordings"), "NVR_CREDENTIAL_KEY_PATH": str(root / "credentials.key")}.items():
            patch = mock.patch.dict(os.environ, {key: value})
            patch.start()
            self.addCleanup(patch.stop)
        (root / "recordings").mkdir()
        self.live = LiveServer(create_app(start_workers=False))
        self.proxy = ReverseProxy(self.live.port)
        self.addCleanup(self.live.close)
        self.addCleanup(self.proxy.close)
        self.direct = httpx.Client(base_url=f"http://127.0.0.1:{self.live.port}", timeout=20)
        self.addCleanup(self.direct.close)
        setup = self.direct.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD})
        self.assertEqual(setup.status_code, 201, setup.text)

    def through_proxy(self) -> httpx.Client:
        client = httpx.Client(base_url=f"http://127.0.0.1:{self.proxy.port}", timeout=20)
        self.addCleanup(client.close)
        return client

    def administrator_through_proxy(self) -> httpx.Client:
        client = self.through_proxy()
        self.assertEqual(client.post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 200)
        return client

    def add_operator(self, name: str = "viewer") -> None:
        with database() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,'operator')", (name, hash_password(OPERATOR_PASSWORD)))

    def reach(self, client: httpx.Client) -> dict:
        response = client.get("/api/admin/remote-access")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class ProxyDetectionTests(ProxiedCase):
    def test_behind_a_trusted_proxy_the_client_address_and_https_are_detected(self):
        self.trust("127.0.0.1")
        shown = self.reach(self.administrator_through_proxy())
        self.assertEqual((shown["client_address"], shown["peer_address"], shown["peer_is_trusted_proxy"]), ("203.0.113.9", "127.0.0.1", True))
        self.assertEqual((shown["https"], shown["host"], shown["remote"]), (True, "nvr.example.test", True))
        self.assertEqual(shown["trusted_proxies"], ["127.0.0.1/32"])

    def test_a_caller_cannot_choose_its_address_through_the_proxy_by_sending_the_header_itself(self):
        self.trust("127.0.0.1")
        client = self.administrator_through_proxy()
        shown = self.reach_with(client, {"X-Forwarded-For": "192.168.1.5, 10.0.0.1"})  # Both claim to be local
        self.assertEqual(shown["client_address"], "203.0.113.9")
        self.assertTrue(shown["remote"])

    def reach_with(self, client: httpx.Client, headers: dict[str, str]) -> dict:
        response = client.get("/api/admin/remote-access", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_headers_sent_straight_to_the_nvr_are_ignored_even_when_a_proxy_is_trusted(self):
        self.trust("10.9.9.0/24")  # Not the loopback peer the direct request comes from
        self.assertEqual(self.direct.post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 200)
        shown = self.reach_with(self.direct, {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example"})
        self.assertEqual((shown["client_address"], shown["https"], shown["host"], shown["peer_is_trusted_proxy"]), ("127.0.0.1", False, f"127.0.0.1:{self.live.port}", False))
        self.assertEqual(shown["remote"], True)  # Forwarded headers from an unlisted peer: the caller is unknown, so remote, not local

    def test_with_no_trusted_proxy_configured_the_proxy_is_just_a_peer_and_settings_says_what_to_do(self):
        shown = self.reach(self.administrator_through_proxy())
        self.assertEqual((shown["client_address"], shown["https"], shown["peer_is_trusted_proxy"], shown["remote"]), ("127.0.0.1", False, False, True))
        codes = [item["code"] for item in shown["warnings"]]
        self.assertIn("forwarded_headers_ignored", codes)
        self.assertIn("172.16.0.0/12", shown["local_networks"])

    def test_a_direct_local_visit_is_local_and_unremarkable(self):
        self.direct.post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD})
        shown = self.reach(self.direct)
        self.assertEqual((shown["client_address"], shown["remote"], shown["https"]), ("127.0.0.1", False, False))
        self.assertEqual(shown["warnings"], [])

    def test_sign_in_limits_count_each_person_behind_the_proxy_separately_only_when_it_is_trusted(self):
        def fail(address: str) -> None:
            self.proxy.client = address
            for _ in range(5):
                self.through_proxy().post("/api/auth/login", json={"username": "keeper", "password": "wrong password 12345"})
        self.trust("127.0.0.1")
        fail("203.0.113.9")
        self.proxy.client = "198.51.100.4"
        self.assertEqual(self.through_proxy().post("/api/auth/login", json={"username": "nobody", "password": "wrong password 12345"}).status_code, 401)  # Another address is not held up
        with database() as db:
            self.assertIn("203.0.113.9", [row[0] for row in db.execute("SELECT key FROM auth_failures WHERE scope='address'")])


class WarningsThroughTheProxyTests(ProxiedCase):
    def test_plain_http_from_outside_the_local_network_is_warned_about_and_https_is_not(self):
        self.trust("127.0.0.1")
        self.proxy.proto = "http"
        client = self.administrator_through_proxy()
        shown = self.reach(client)
        self.assertEqual(shown["warnings"][0]["code"], "plain_http_remote")
        self.assertEqual(shown["warnings"][0]["severity"], "bad")
        self.proxy.proto = "https"
        self.assertNotIn("plain_http_remote", [item["code"] for item in self.reach(client)["warnings"]])

    def test_plain_http_from_the_local_network_is_not_warned_about(self):
        self.trust("127.0.0.1")
        self.proxy.proto, self.proxy.client = "http", "192.168.1.50"
        shown = self.reach(self.administrator_through_proxy())
        self.assertEqual((shown["remote"], shown["https"]), (False, False))
        self.assertNotIn("plain_http_remote", [item["code"] for item in shown["warnings"]])

    def test_only_administrators_can_see_how_the_nvr_is_reached(self):
        self.add_operator()
        client = httpx.Client(base_url=f"http://127.0.0.1:{self.live.port}", timeout=20)
        self.addCleanup(client.close)
        self.assertEqual(client.get("/api/admin/remote-access").status_code, 401)
        client.post("/api/auth/login", json={"username": "viewer", "password": OPERATOR_PASSWORD})
        self.assertEqual(client.get("/api/admin/remote-access").status_code, 403)


class LiveSocketTests(ProxiedCase):
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.proxy.port}/api/cameras/{'0' * 8}/live/stream"

    def test_the_live_socket_works_through_the_proxy_and_refuses_foreign_origins(self):
        self.trust("127.0.0.1")
        with connect(self.url(), origin="https://nvr.example.test", open_timeout=15) as socket:
            self.assertEqual(json.loads(socket.recv(timeout=15))["type"], "unauthorized")  # Past the origin check, stopped for want of a session
        for foreign in ("https://evil.example", "https://nvr.example.test.evil.example", "http://127.0.0.1:1"):
            with self.assertRaises(InvalidStatus) as refused:
                connect(self.url(), origin=foreign, open_timeout=15)
            self.assertEqual(refused.exception.response.status_code, 403, foreign)

    def test_without_a_trusted_proxy_the_rewritten_host_does_not_match_and_the_socket_is_refused(self):
        with self.assertRaises(InvalidStatus):
            connect(self.url(), origin="https://nvr.example.test", open_timeout=15)

    def test_a_listed_origin_is_accepted_and_a_client_that_sends_no_origin_is_not_a_browser(self):
        os.environ["NVR_ALLOWED_ORIGINS"] = "https://nvr.example.test"
        with connect(self.url(), origin="https://nvr.example.test", open_timeout=15) as socket:
            self.assertEqual(json.loads(socket.recv(timeout=15))["type"], "unauthorized")
        with connect(self.url(), open_timeout=15) as socket:
            self.assertEqual(json.loads(socket.recv(timeout=15))["type"], "unauthorized")

    def test_a_socket_straight_to_the_nvr_still_needs_the_same_site(self):
        direct = f"ws://127.0.0.1:{self.live.port}/api/cameras/{'0' * 8}/live/stream"
        with connect(direct, origin=f"http://127.0.0.1:{self.live.port}", open_timeout=15) as socket:
            self.assertEqual(json.loads(socket.recv(timeout=15))["type"], "unauthorized")
        with self.assertRaises(InvalidStatus):
            connect(direct, origin="https://evil.example", open_timeout=15)


class RemoteSecondFactorTests(ProxiedCase):
    def set_policy(self, **payload) -> httpx.Response:
        self.direct.post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD})
        return self.direct.put("/api/admin/security", json={"mfa_policy": "off", **payload})

    def sign_in(self, client: httpx.Client, name: str = "viewer") -> dict:
        response = client.post("/api/auth/login", json={"username": name, "password": OPERATOR_PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_the_requirement_is_set_by_an_administrator_and_audited_and_off_by_default(self):
        self.direct.post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD})
        self.assertEqual(self.direct.get("/api/admin/security").json(), {"mfa_policy": "off", "remote_mfa": False})
        self.assertEqual(self.set_policy(remote_mfa=True).json(), {"mfa_policy": "off", "remote_mfa": True})
        self.assertEqual(self.direct.put("/api/admin/security", json={"mfa_policy": "all"}).json(), {"mfa_policy": "all", "remote_mfa": True})  # Left as it was when absent
        with database() as db:
            events = [(row["action"], json.loads(row["details_json"])) for row in db.execute("SELECT action,details_json FROM audit_log WHERE action IN ('remote_mfa_changed','mfa_policy_changed') ORDER BY id")]
        self.assertEqual(events[0][0], "remote_mfa_changed")
        self.assertEqual((events[0][1]["before"], events[0][1]["after"]), (False, True))

    def test_a_remote_session_without_a_second_factor_reaches_only_enrolment_while_a_local_one_does_not(self):
        self.trust("127.0.0.1")
        self.add_operator()
        self.set_policy(remote_mfa=True)
        local = httpx.Client(base_url=f"http://127.0.0.1:{self.live.port}", timeout=20)
        self.addCleanup(local.close)
        self.assertNotIn("mfa_enrolment_required", self.sign_in(local))
        self.assertEqual(local.get("/api/cameras").status_code, 200)
        self.proxy.client = "203.0.113.9"
        remote = self.through_proxy()
        self.assertTrue(self.sign_in(remote)["mfa_enrolment_required"])
        refused = remote.get("/api/cameras")
        self.assertEqual((refused.status_code, refused.json()["detail"]), (403, "Set up a second factor to continue"))
        self.assertEqual(remote.get("/api/me/security").status_code, 200)  # Enrolment stays reachable
        self.assertTrue(remote.get("/api/me/security").json()["remote_required"])

    def test_a_session_started_locally_is_asked_as_soon_as_it_is_used_from_outside(self):
        self.trust("127.0.0.1")
        self.add_operator()
        self.set_policy(remote_mfa=True)
        self.proxy.client = "192.168.1.60"  # Local, through the proxy
        client = self.through_proxy()
        self.assertNotIn("mfa_enrolment_required", self.sign_in(client))
        self.assertEqual(client.get("/api/cameras").status_code, 200)
        self.proxy.client = "203.0.113.9"  # The same session cookie, now arriving from outside
        self.assertEqual(client.get("/api/cameras").status_code, 403)
        self.proxy.client = "192.168.1.60"
        self.assertEqual(client.get("/api/cameras").status_code, 200)

    def test_a_proxy_that_was_not_listed_cannot_be_used_to_look_local(self):
        self.add_operator()
        self.set_policy(remote_mfa=True)
        client = self.through_proxy()
        self.proxy.client = "192.168.1.60"  # Claims to be local, but its headers come from a peer nobody listed
        self.assertTrue(self.sign_in(client)["mfa_enrolment_required"])

    def test_an_account_with_a_second_factor_is_asked_for_it_as_before_and_without_the_requirement_nothing_changes(self):
        self.trust("127.0.0.1")
        self.add_operator()
        remote = self.through_proxy()
        self.assertNotIn("mfa_enrolment_required", self.sign_in(remote))
        self.assertEqual(remote.get("/api/cameras").status_code, 200)

    def test_entra_accounts_are_never_asked_here(self):
        from app import mfa
        self.assertFalse(mfa.required_for("off", "operator", local=False, remote_policy=True, remote=True))
        self.assertTrue(mfa.required_for("off", "operator", local=True, remote_policy=True, remote=True))
        self.assertFalse(mfa.required_for("off", "operator", local=True, remote_policy=True, remote=False))
        self.assertFalse(mfa.required_for("off", "operator", local=True, remote_policy=False, remote=True))


class DocumentedSetupTests(unittest.TestCase):
    """Each documented tunnel setup ships as a Compose override and is listed as a post-merge check with its steps."""
    ROOT = Path(__file__).resolve().parent.parent
    SETUPS = {"Tailscale": "compose.tailscale.yaml", "Cloudflare Tunnel": "compose.cloudflared.yaml", "WireGuard": "compose.wireguard.yaml"}

    def test_every_setup_has_a_compose_override_that_tells_the_nvr_who_to_trust(self):
        import yaml
        for name, filename in self.SETUPS.items():
            document = yaml.safe_load((self.ROOT / filename).read_text(encoding="utf-8"))
            environment = document["services"]["nvr"]["environment"]
            if name == "WireGuard":
                self.assertIn("NVR_LOCAL_NETWORKS", environment)  # No proxy: the tunnel's subnet is the local network
                self.assertNotIn("NVR_TRUSTED_PROXIES", environment)
            else:
                self.assertEqual(environment["NVR_TRUSTED_PROXIES"], "127.0.0.1", name)  # The tunnel runs on the host network and reaches the NVR on loopback
                self.assertTrue(environment["NVR_PUBLIC_URL"].startswith("https://"), name)
                self.assertEqual(environment["NVR_COOKIE_SECURE"], "true", name)
            self.assertEqual(len([service for service in document["services"] if service != "nvr"]), 1, name)
        base = yaml.safe_load((self.ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]["nvr"]["environment"]
        for variable in ("NVR_TRUSTED_PROXIES", "NVR_LOCAL_NETWORKS", "NVR_ALLOWED_ORIGINS"):
            self.assertIn(variable, base)
        self.assertEqual(base["NVR_TRUSTED_PROXIES"], "${NVR_TRUSTED_PROXIES:-}")  # Nothing is trusted unless it is set

    def test_the_tailscale_serve_config_points_at_the_nvr_and_nothing_else(self):
        config = json.loads((self.ROOT / "deploy" / "tailscale-serve.json").read_text(encoding="utf-8"))
        self.assertEqual([handler["Proxy"] for web in config["Web"].values() for handler in web["Handlers"].values()], ["http://127.0.0.1:8280"])

    def test_each_setup_is_a_post_merge_check_with_numbered_steps_and_a_results_row(self):
        checks = (self.ROOT / "docs" / "acceptance" / "remote-access.md").read_text(encoding="utf-8")
        guide = (self.ROOT / "docs" / "remote-access.md").read_text(encoding="utf-8")
        for name, filename in self.SETUPS.items():
            section = checks.split(f"## {name} (`{filename}`)")[1].split("\n## ")[0]
            self.assertGreaterEqual(section.count("\n1. "), 1, name)
            self.assertGreaterEqual(section.count("- [ ]") + section.count("[ ]"), 4, name)
            self.assertIn(f"| {name} |", checks.split("## Results")[1], name)
            self.assertIn(filename, guide, name)
            self.assertIn("## " + name, guide, name)

    def test_the_guide_names_every_variable_and_every_warning_settings_can_show(self):
        guide = (self.ROOT / "docs" / "remote-access.md").read_text(encoding="utf-8")
        for variable in ("NVR_TRUSTED_PROXIES", "NVR_LOCAL_NETWORKS", "NVR_ALLOWED_ORIGINS", "NVR_PUBLIC_URL", "NVR_COOKIE_SECURE", "NVR_TRUST_FORWARDED_FOR"):
            self.assertIn(variable, guide)
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        for variable in ("NVR_TRUSTED_PROXIES", "NVR_LOCAL_NETWORKS", "NVR_ALLOWED_ORIGINS"):
            self.assertIn(f"`{variable}`", readme)


if __name__ == "__main__":
    unittest.main()
