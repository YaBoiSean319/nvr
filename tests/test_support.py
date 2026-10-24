"""Ephemeral credentials and non-routable addresses for isolated tests."""

from contextlib import contextmanager
from secrets import token_urlsafe
from unittest.mock import patch


# The account password policy needs a letter and a number; a random token alone can lack a digit.
TEST_PASSWORD = f"fixture-{token_urlsafe(24)}-7"
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


@contextmanager
def patch_camera_probe(**kwargs):
    """Replace the stream probe everywhere cameras are tested before saving."""
    with patch("app.api.cameras.probe_stream", **kwargs) as probe, patch("app.api.onboarding.probe_stream", new=probe):
        yield probe
