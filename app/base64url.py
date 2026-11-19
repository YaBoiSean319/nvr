"""URL-safe base64 without padding: the form passkeys, web push, push tokens and sign-in tokens use.

One copy (#387). ``decode`` accepts text with or without padding and raises what the standard decoder raises (``binascii.Error``, a
``ValueError``); a caller with its own error to give wraps it."""
from __future__ import annotations

import base64


def encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
