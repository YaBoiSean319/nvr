"""Encrypt camera connection strings at rest; never serialize plaintext to API responses."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .store import database_path


def key_path() -> Path:
    return Path(os.environ.get("NVR_CREDENTIAL_KEY_PATH", str(database_path().with_name("credentials.key"))))


def _fernet() -> Fernet:
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        key = Fernet.generate_key()
        fd, temporary_name = tempfile.mkstemp(prefix="credentials-", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_name, path)
            except FileExistsError:
                key = path.read_bytes()
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    if len(key) != 44:
        raise RuntimeError("Camera credential key is missing or invalid")
    os.chmod(path, 0o600)
    return Fernet(key)


def encrypt(value: str) -> bytes:
    return _fernet().encrypt(value.encode("utf-8"))


def decrypt(value: bytes) -> str:
    try:
        return _fernet().decrypt(value).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise RuntimeError("Unable to decrypt camera credentials") from exc
