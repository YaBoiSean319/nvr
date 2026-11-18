"""Encrypt camera connection strings at rest; never serialize plaintext to API responses."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .store import database_path


def key_path() -> Path:
    return Path(os.environ.get("NVR_CREDENTIAL_KEY_PATH", str(database_path().with_name("credentials.key"))))


_loaded: dict[str, tuple[tuple[int, int, int], bytes, Fernet]] = {}  # Per key file: what it looked like when read, its bytes, the cipher
logger = logging.getLogger("nvr.credentials")


def _key() -> tuple[bytes, Fernet]:
    """The credential key and its cipher, read once and kept until the file changes.

    Every encrypt and decrypt used to read the file again and re-check its folder and permissions: listing 200 cameras spent about two thirds
    of its time doing that (#383). One look at the file's size, time and identity now says whether what is held is still right."""
    path = key_path()
    try:
        found = path.stat()
        seen = (found.st_mtime_ns, found.st_size, found.st_ino)
        held = _loaded.get(str(path))
        if held is not None and held[0] == seen:
            return held[1], held[2]
    except FileNotFoundError:
        pass
    key = _read_or_create(path)
    found = path.stat()
    _loaded[str(path)] = ((found.st_mtime_ns, found.st_size, found.st_ino), key, Fernet(key))
    return key, _loaded[str(path)][2]


def _read_or_create(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        _warn_if_secrets_exist(path)
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
    return key


def _warn_if_secrets_exist(path: Path) -> None:
    """Say so, loudly, when a new key is about to be made on an installation that already has cameras: everything stored was encrypted with a key
    that is not here (the data volume was restored or moved without the secrets volume), and nothing saved from now on can be read if the old
    key is put back."""
    try:
        import sqlite3

        database = database_path()
        if not database.exists():
            return
        db = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1)
        try:
            has_cameras = db.execute("SELECT 1 FROM cameras LIMIT 1").fetchone() is not None
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - a warning must never stop the key being made
        return
    if has_cameras:
        logger.error("THE CREDENTIAL KEY FILE %s IS MISSING and a new one is being created, although this installation already has cameras. "
                     "Their stored passwords and addresses cannot be read with the new key. If the data was restored or moved, stop the NVR and "
                     "put the original key file back before saving anything (see docs/database-recovery.md).", path)


def _fernet() -> Fernet:
    return _key()[1]


def ensure_key() -> Path:
    """Create the credential key if this installation has none yet, and return its path."""
    _fernet()
    return key_path()


def derived_key(purpose: str) -> bytes:
    """A key for one named purpose, derived from the protected credential key."""
    return hmac.new(_key()[0], b"nvr-derived-key:" + purpose.encode("ascii"), hashlib.sha256).digest()


def encrypt(value: str) -> bytes:
    return _fernet().encrypt(value.encode("utf-8"))


def decrypt(value: bytes) -> str:
    try:
        return _fernet().decrypt(value).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise RuntimeError("Unable to decrypt camera credentials") from exc
