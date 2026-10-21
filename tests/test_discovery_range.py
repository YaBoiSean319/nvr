import unittest

from app.discovery import IP_RANGE_MAX_HOSTS, parse_camera_ip_range


class CameraRangeTests(unittest.TestCase):
    def test_accepts_private_ranges_with_bounded_host_count(self):
        hosts = parse_camera_ip_range("10.45.50.0/24")
        self.assertEqual(len(hosts), 254)
        self.assertEqual(str(hosts[0]), "10.45.50.1")
        self.assertEqual(str(hosts[-1]), "10.45.50.254")
        self.assertEqual(len(parse_camera_ip_range("192.168.1.7/32")), 1)

    def test_rejects_public_oversized_and_ipv6_ranges(self):
        for value in ("8.8.8.0/24", "10.0.0.0/16", "2001:db8::/120", "not-a-range"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_camera_ip_range(value)
        self.assertGreater(IP_RANGE_MAX_HOSTS, 0)


if __name__ == "__main__":
    unittest.main()
