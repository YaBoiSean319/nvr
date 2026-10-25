"""WebAuthn (passkeys), verified here so the NVR needs no further dependency.

Registration accepts any attestation format and ignores the attestation statement: the NVR
asks for none, and a passkey here only has to prove it holds its private key. Assertions are
checked in full: the ceremony type, challenge, origin, relying party, user presence (and
verification when asked), signature and signature counter. Supported algorithms are ES256
(P-256), RS256 and EdDSA (Ed25519).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import cbor

ES256, RS256, EDDSA = -7, -257, -8
SUPPORTED = (ES256, RS256, EDDSA)
FLAG_USER_PRESENT, FLAG_USER_VERIFIED, FLAG_ATTESTED = 0x01, 0x04, 0x40
CHALLENGE_BYTES = 32


class WebAuthnError(ValueError):
    """A ceremony that must be refused; the message is safe to show."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(value: str) -> bytes:
    if not isinstance(value, str):
        raise WebAuthnError("The passkey response is malformed")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise WebAuthnError("The passkey response is malformed") from exc


def new_challenge() -> bytes:
    return secrets.token_bytes(CHALLENGE_BYTES)


@dataclass(frozen=True)
class RelyingParty:
    id: str
    origin: str


def relying_party(request_origin: str) -> RelyingParty:
    """The relying party for this installation.

    ``NVR_PUBLIC_URL`` decides when it is set; otherwise the origin the browser used for this
    request does. ``NVR_WEBAUTHN_RP_ID`` can name a parent domain to share passkeys between hosts."""
    public = os.environ.get("NVR_PUBLIC_URL", "").strip().rstrip("/")
    origin = public or request_origin
    parts = urlsplit(origin)
    if not parts.hostname or parts.scheme not in {"http", "https"}:
        raise WebAuthnError("Passkeys need the NVR to be opened at a web address")
    rp_id = os.environ.get("NVR_WEBAUTHN_RP_ID", "").strip() or parts.hostname
    if not (parts.hostname == rp_id or parts.hostname.endswith("." + rp_id)):
        raise WebAuthnError("NVR_WEBAUTHN_RP_ID does not match this address")
    return RelyingParty(rp_id, f"{parts.scheme}://{parts.netloc}")


def creation_options(rp: RelyingParty, challenge: bytes, user_id: int, username: str, existing: list[str]) -> dict[str, Any]:
    return {
        "challenge": b64url(challenge),
        "rp": {"id": rp.id, "name": "Workplace NVR"},
        "user": {"id": b64url(str(user_id).encode()), "name": username, "displayName": username},
        "pubKeyCredParams": [{"type": "public-key", "alg": alg} for alg in SUPPORTED],
        "excludeCredentials": [{"type": "public-key", "id": credential} for credential in existing],
        "authenticatorSelection": {"residentKey": "preferred", "userVerification": "preferred"},
        "attestation": "none",
        "timeout": 120_000,
    }


def request_options(rp: RelyingParty, challenge: bytes, allowed: list[dict[str, Any]], user_verification: str) -> dict[str, Any]:
    options: dict[str, Any] = {"challenge": b64url(challenge), "rpId": rp.id, "userVerification": user_verification, "timeout": 120_000}
    if allowed:
        options["allowCredentials"] = [{"type": "public-key", "id": item["credential_id"], "transports": item.get("transports", [])} for item in allowed]
    return options


def _client_data(response: dict[str, Any], expected_type: str, challenge: bytes, rp: RelyingParty) -> bytes:
    raw = unb64url(response.get("clientDataJSON", ""))
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise WebAuthnError("The passkey response is malformed") from exc
    if not isinstance(data, dict) or data.get("type") != expected_type:
        raise WebAuthnError("The passkey response is for something else")
    if not secrets.compare_digest(str(data.get("challenge", "")), b64url(challenge)):
        raise WebAuthnError("The passkey response does not answer this request")
    if data.get("origin") != rp.origin:
        raise WebAuthnError("The passkey was made for a different web address")
    if data.get("crossOrigin") is True:
        raise WebAuthnError("Passkeys cannot be used from an embedded page")
    return raw


@dataclass(frozen=True)
class _AuthData:
    rp_id_hash: bytes
    flags: int
    sign_count: int
    credential_id: bytes | None
    cose_key: bytes | None


def _parse_auth_data(data: bytes) -> _AuthData:
    if len(data) < 37:
        raise WebAuthnError("The passkey response is malformed")
    rp_id_hash, flags, sign_count = data[:32], data[32], int.from_bytes(data[33:37], "big")
    credential_id = cose_key = None
    if flags & FLAG_ATTESTED:
        if len(data) < 55:
            raise WebAuthnError("The passkey response is malformed")
        length = int.from_bytes(data[53:55], "big")
        if length == 0 or length > 1023 or len(data) < 55 + length:
            raise WebAuthnError("The passkey response is malformed")
        credential_id = data[55:55 + length]
        try:
            _, end = cbor.decode_first(data, 55 + length)
        except cbor.CborError as exc:
            raise WebAuthnError("The passkey response is malformed") from exc
        cose_key = data[55 + length:end]
    return _AuthData(rp_id_hash, flags, sign_count, credential_id, cose_key)


def _check_relying_party(auth: _AuthData, rp: RelyingParty, require_verified: bool) -> None:
    if not secrets.compare_digest(auth.rp_id_hash, hashlib.sha256(rp.id.encode()).digest()):
        raise WebAuthnError("The passkey was made for a different site")
    if not auth.flags & FLAG_USER_PRESENT:
        raise WebAuthnError("The passkey did not confirm that you were there")
    if require_verified and not auth.flags & FLAG_USER_VERIFIED:
        raise WebAuthnError("The passkey did not verify you with a PIN or biometric")


@dataclass(frozen=True)
class NewPasskey:
    credential_id: str
    public_key: bytes
    algorithm: int
    sign_count: int
    transports: list[str]


def verify_registration(credential: dict[str, Any], challenge: bytes, rp: RelyingParty) -> NewPasskey:
    """A new passkey from the browser's ``navigator.credentials.create`` result."""
    try:
        response = credential["response"]
        _client_data(response, "webauthn.create", challenge, rp)
        attestation = cbor.decode(unb64url(response["attestationObject"]))
        auth = _parse_auth_data(attestation["authData"])
    except (KeyError, TypeError, cbor.CborError) as exc:
        raise WebAuthnError("The passkey response is malformed") from exc
    _check_relying_party(auth, rp, require_verified=False)
    if auth.credential_id is None or auth.cose_key is None:
        raise WebAuthnError("The response carries no new passkey")
    if b64url(auth.credential_id) != credential.get("id"):
        raise WebAuthnError("The passkey response is inconsistent")
    key = cbor.decode(auth.cose_key)
    algorithm = key.get(3)
    if algorithm not in SUPPORTED:
        raise WebAuthnError("This passkey uses an algorithm the NVR does not support")
    _public_key(auth.cose_key)  # Refuses a key that is malformed.
    transports = response.get("transports")
    return NewPasskey(b64url(auth.credential_id), auth.cose_key, int(algorithm), auth.sign_count,
                      [item for item in transports if isinstance(item, str)][:8] if isinstance(transports, list) else [])


def _public_key(cose: bytes) -> Any:
    try:
        key = cbor.decode(cose)
        algorithm = key[3]
        if algorithm == ES256:
            if key[1] != 2 or key[-1] != 1:
                raise WebAuthnError("This passkey's key is not a P-256 key")
            return ec.EllipticCurvePublicNumbers(int.from_bytes(key[-2], "big"), int.from_bytes(key[-3], "big"), ec.SECP256R1()).public_key()
        if algorithm == RS256:
            if key[1] != 3:
                raise WebAuthnError("This passkey's key is not an RSA key")
            return rsa.RSAPublicNumbers(int.from_bytes(key[-2], "big"), int.from_bytes(key[-1], "big")).public_key()
        if algorithm == EDDSA:
            if key[1] != 1 or key[-1] != 6:
                raise WebAuthnError("This passkey's key is not an Ed25519 key")
            return Ed25519PublicKey.from_public_bytes(key[-2])
    except (KeyError, TypeError, ValueError, cbor.CborError) as exc:
        raise WebAuthnError("This passkey's key is malformed") from exc
    raise WebAuthnError("This passkey uses an algorithm the NVR does not support")


def verify_assertion(credential: dict[str, Any], challenge: bytes, rp: RelyingParty, public_key: bytes, stored_sign_count: int,
                     require_verified: bool) -> int:
    """Check a ``navigator.credentials.get`` result against a stored passkey; returns the new signature counter."""
    try:
        response = credential["response"]
        client_data = _client_data(response, "webauthn.get", challenge, rp)
        authenticator_data = unb64url(response["authenticatorData"])
        signature = unb64url(response["signature"])
    except (KeyError, TypeError) as exc:
        raise WebAuthnError("The passkey response is malformed") from exc
    auth = _parse_auth_data(authenticator_data)
    _check_relying_party(auth, rp, require_verified)
    key = _public_key(public_key)
    signed = authenticator_data + hashlib.sha256(client_data).digest()
    algorithm = cbor.decode(public_key)[3]
    try:
        if algorithm == ES256:
            key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        elif algorithm == RS256:
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            key.verify(signature, signed)
    except (InvalidSignature, ValueError) as exc:
        raise WebAuthnError("The passkey's signature is not valid") from exc
    if (auth.sign_count or stored_sign_count) and auth.sign_count <= stored_sign_count:
        raise WebAuthnError("This passkey's counter went backwards: it may have been copied")
    return auth.sign_count

