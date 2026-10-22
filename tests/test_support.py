"""Ephemeral credentials and non-routable addresses for isolated tests."""

from secrets import token_urlsafe


TEST_PASSWORD = f"fixture-{token_urlsafe(24)}"
TEST_OIDC_CLIENT_SECRET = token_urlsafe(24)
TEST_OIDC_SESSION_SECRET = token_urlsafe(32)
TEST_CAMERA_PASSWORD = token_urlsafe(24)
TEST_URL_TOKEN = token_urlsafe(18)

# RFC 5737 TEST-NET-2 addresses are documentation-only and never routed.
TEST_CAMERA_IP = "198.51.100.25"
TEST_CAMERA_IP_A = "198.51.100.10"
TEST_CAMERA_IP_B = "198.51.100.11"
TEST_CAMERA_IP_C = "198.51.100.12"
TEST_CAMERA_IP_D = "198.51.100.20"
TEST_CAMERA_IP_E = "198.51.100.21"
