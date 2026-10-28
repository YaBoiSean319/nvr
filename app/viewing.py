"""Live-viewing admission limits shared by layout validation and media delivery."""
from __future__ import annotations

MAX_LIVE_VIEWERS = 16
MAX_LIVE_AUDIO_LISTENERS = 16
# H.265 converted to H.264 for browsers that cannot play it: distinct conversions at once (each is shared by every viewer of that stream).
MAX_LIVE_TRANSCODES = 4
