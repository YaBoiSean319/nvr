from __future__ import annotations

import unittest

from tests.browser.server import FixtureDiscoveryResponder


class FixtureDiscoveryResponderTest(unittest.TestCase):
    def test_only_loopback_onvif_probes_are_answered(self) -> None:
        matching_probe = b"<Probe><Types>NetworkVideoTransmitter</Types></Probe>"

        self.assertTrue(FixtureDiscoveryResponder.accepts_datagram(matching_probe, ("127.0.0.1", 3702)))
        self.assertFalse(FixtureDiscoveryResponder.accepts_datagram(matching_probe, ("127.0.0.2", 3702)))
        self.assertFalse(FixtureDiscoveryResponder.accepts_datagram(b"unrelated UDP discovery payload", ("127.0.0.1", 3702)))


if __name__ == "__main__":
    unittest.main()
