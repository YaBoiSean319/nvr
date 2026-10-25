"""A software WebAuthn authenticator: builds the attestations and assertions a browser would send."""
from __future__ import annotations

import hashlib
import json
import os
import struct
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from app import cbor
from app.webauthn import b64url, unb64url

UP, UV, AT = 0x01, 0x04, 0x40


class SoftwareAuthenticator:
    def __init__(self, rp_id: str = "testserver", origin: str = "http://testserver") -> None:
        self.rp_id, self.origin = rp_id, origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0

    def cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor.encode({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})

    def _client_data(self, kind: str, challenge: str, origin: str | None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin, "crossOrigin": False}).encode()

    def create(self, options: dict[str, Any], *, user_verified: bool = True, origin: str | None = None, rp_id: str | None = None) -> dict[str, Any]:
        """What ``navigator.credentials.create`` returns for these options."""
        flags = UP | AT | (UV if user_verified else 0)
        auth_data = (hashlib.sha256((rp_id or self.rp_id).encode()).digest() + bytes([flags]) + struct.pack(">I", self.sign_count)
                     + bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + self.cose_key())
        return {"id": b64url(self.credential_id), "rawId": b64url(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64url(self._client_data("webauthn.create", options["challenge"], origin)),
                             "attestationObject": b64url(cbor.encode({"fmt": "none", "attStmt": {}, "authData": auth_data})),
                             "transports": ["internal"]}}

    def get(self, options: dict[str, Any], *, user_verified: bool = True, origin: str | None = None, sign_count: int | None = None,
            present: bool = True) -> dict[str, Any]:
        """What ``navigator.credentials.get`` returns for these options."""
        self.sign_count = self.sign_count + 1 if sign_count is None else sign_count
        flags = (UP if present else 0) | (UV if user_verified else 0)
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.sign_count)
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64url(self.credential_id), "rawId": b64url(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64url(client_data), "authenticatorData": b64url(auth_data), "signature": b64url(signature),
                             "userHandle": None}}


def decode_secret(text: str) -> bytes:
    import base64
    compact = "".join(text.split())
    return base64.b32decode(compact + "=" * (-len(compact) % 8))
