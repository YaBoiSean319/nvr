import os
import unittest
from unittest.mock import patch

from app.entrypoint import listen_port


class EntrypointTests(unittest.TestCase):
    def test_default_listener_matches_host_network_compose(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(listen_port(), 8280)

    def test_listener_can_move_away_from_a_host_port_conflict(self):
        with patch.dict(os.environ, {"NVR_LISTEN_PORT": "18280"}, clear=True):
            self.assertEqual(listen_port(), 18280)

    def test_listener_rejects_invalid_ports(self):
        for value in ("zero", "0", "65536"):
            with self.subTest(value=value), patch.dict(os.environ, {"NVR_LISTEN_PORT": value}, clear=True):
                with self.assertRaises(SystemExit):
                    listen_port()


if __name__ == "__main__":
    unittest.main()
