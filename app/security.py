"""Small, dependency-free password and session-token primitives."""
from __future__ import annotations

import hashlib
import hmac
import secrets

_ITERATIONS = 600_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


_decoy: str | None = None


def spend_password_time(password: str) -> None:
    """Do the work of checking a password against nothing, so refusing an unknown name takes as long as a wrong password."""
    global _decoy
    if _decoy is None:
        _decoy = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _decoy)
