"""Which webhook destinations are accepted, and the message for each refusal, pinned case by case (#286: the validator is restructured without changing any of this)."""
from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from app.webhooks import validate_target_url

NAMES = {
    "public.example": ["93.184.216.34"],
    "private.example": ["10.0.0.5"],
    "mixed.example": ["93.184.216.34", "10.0.0.5"],
    "loop.example": ["127.0.0.1"],
    "cgnat.example": ["100.64.0.1"],
    "v6public.example": ["2001:4860:4860::8888"],
}


def resolver(host, port, type=0):  # noqa: A002 - mirrors socket.getaddrinfo
    if host not in NAMES:
        raise socket.gaierror("unknown")
    return [(socket.AF_INET6 if ":" in address else socket.AF_INET, type, 0, "", (address, port)) for address in NAMES[host]]


class TargetPolicyTests(unittest.TestCase):
    def check(self, url: str, allow_private: bool, result: str | None = None, refused: str | None = None) -> None:
        with patch("app.webhooks.socket.getaddrinfo", resolver):
            if refused is not None:
                with self.assertRaises(ValueError, msg=url) as caught:
                    validate_target_url(url, allow_private)
                self.assertIn(refused, str(caught.exception), url)
            else:
                self.assertEqual(validate_target_url(url, allow_private), result, url)

    def test_what_is_accepted_and_how_it_is_written_back(self):
        self.check("https://public.example/hook", False, "https://public.example/hook")
        self.check("  https://public.example  ", False, "https://public.example/")  # Trimmed, and an empty path becomes /
        self.check("https://public.example./hook?a=1&b=2", False, "https://public.example/hook?a=1&b=2")  # A trailing dot goes, the query stays
        self.check("https://public.example:8443/x", False, "https://public.example:8443/x")
        self.check("https://93.184.216.34/x", False, "https://93.184.216.34/x")
        self.check("https://[2001:4860:4860::8888]:444/x", False, "https://[2001:4860:4860::8888]:444/x")
        self.check("https://v6public.example/x", False, "https://v6public.example/x")
        self.check("https://10.0.0.5/x", True, "https://10.0.0.5/x")
        self.check("http://10.0.0.5:8080/x", True, "http://10.0.0.5:8080/x")  # Plain http only to a private-only address that was explicitly allowed
        self.check("http://192.168.1.20/x", True, "http://192.168.1.20/x")

    def test_a_malformed_url_is_refused(self):
        for url in ("http://[::1", "https://public.example:99999/x"):
            self.check(url, False, refused="Webhook URL is invalid")
        self.check("https://public.example:0/x", False, refused="Webhook port is invalid")
        for url in ("ftp://public.example/x", "https:///x", "https://user:pw@public.example/x", "https://user@public.example/x", "https://public.example/x#frag", "public.example/x", ""):
            self.check(url, False, refused="must be an HTTP(S) destination without embedded credentials or a fragment")

    def test_loopback_link_local_multicast_unspecified_and_reserved_are_never_allowed(self):
        for url in ("https://127.0.0.1/x", "https://[::1]/x", "https://169.254.169.254/latest", "https://[fe80::1]/x", "https://224.0.0.1/x", "https://0.0.0.0/x",
                    "https://240.0.0.1/x", "https://loop.example/x"):
            for allow_private in (False, True):
                self.check(url, allow_private, refused="Loopback, link-local, multicast, and reserved webhook destinations are not allowed")

    def test_other_special_purpose_ranges_are_refused(self):
        for url in ("https://100.64.0.1/x", "https://cgnat.example/x"):
            self.check(url, True, refused="Non-public special-purpose webhook destinations are not allowed")

    def test_private_receivers_need_a_literal_address_and_the_administrators_say_so(self):
        self.check("https://10.0.0.5/x", False, refused="require explicit administrator authorization and a private-only DNS result")
        self.check("https://private.example/x", True, refused="Private webhook receivers must use an explicit private IP address")
        self.check("https://private.example/x", False, refused="Private webhook receivers must use an explicit private IP address")
        self.check("https://mixed.example/x", True, refused="Private webhook receivers must use an explicit private IP address")

    def test_public_destinations_must_use_https(self):
        self.check("http://public.example/x", False, refused="Public webhook destinations must use HTTPS")
        self.check("http://93.184.216.34/x", True, refused="Public webhook destinations must use HTTPS")  # Allowing private ranges does not allow plain http to a public one

    def test_a_name_that_does_not_resolve_is_refused(self):
        self.check("https://nowhere.example/x", False, refused="Webhook hostname could not be resolved")


if __name__ == "__main__":
    unittest.main()
