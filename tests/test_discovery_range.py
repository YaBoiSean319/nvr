import unittest

from app.discovery import IP_RANGE_MAX_HOSTS, parse_camera_ip_range
from app.onvif_stream import _stream_lookup_inputs


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

    def test_custom_onvif_path_must_stay_on_entered_camera(self):
        address, username, urls = _stream_lookup_inputs(
            "10.45.50.25", "operator", "secret", "http://10.45.50.25:8080/custom/onvif", "hikvision"
        )
        self.assertEqual(str(address), "10.45.50.25")
        self.assertEqual(username, "operator")
        self.assertEqual(urls, ("http://10.45.50.25:8080/custom/onvif",))
        with self.assertRaisesRegex(ValueError, "host must match"):
            _stream_lookup_inputs("10.45.50.25", "", "", "http://example.test/onvif/device_service", "auto")


if __name__ == "__main__":
    unittest.main()
