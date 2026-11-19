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


class CredentialKeyMissing(RuntimeError):
    """The key file is gone and the database holds values encrypted with it: no new key is made in its place."""


MISSING_KEY_MESSAGE = ("The NVR's credential key file is missing, and the stored camera passwords and addresses cannot be read without it. "
                       "Put the original key file back (see docs/database-recovery.md); nothing has been changed.")


def _read_or_create(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        if _stored_secrets_exist():
            # A new key here would be made without a word, everything saved from then on would be encrypted with it, and all of that
            # would be lost when the real key was put back (#383). This is what restoring the data without the secrets looks like.
            _say_once(path)
            raise CredentialKeyMissing(MISSING_KEY_MESSAGE) from None
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


_said: set[str] = set()


def _stored_secrets_exist() -> bool:
    """Whether the database already holds something encrypted with a credential key. Read-only, and never the reason a key cannot be made:
    a database that is not there yet, or cannot be read, counts as holding nothing.

    Every column that holds such a value is named ``..._ciphertext``, and every such value begins the same way (the cipher's version byte,
    as text), so a value that is really encrypted can be told from the stand-in bytes a half-made row may hold."""
    import sqlite3

    database = database_path()
    if not database.exists():
        return False
    try:
        db = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return False
    try:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name<>'cameras', name")]
        for table in tables:
            for column in [row[1] for row in db.execute(f'PRAGMA table_info("{table}")') if str(row[1]).endswith("_ciphertext")]:
                if db.execute(f'SELECT 1 FROM "{table}" WHERE CAST(substr("{column}",1,5) AS TEXT)=\'gAAAA\' LIMIT 1').fetchone():
                    return True
        return False
    except sqlite3.Error:
        return False
    finally:
        db.close()


def _say_once(path: Path) -> None:
    if str(path) not in _said:
        _said.add(str(path))
        logger.error("THE CREDENTIAL KEY FILE %s IS MISSING, and this installation already has stored camera passwords and addresses encrypted "
                     "with it. No new key was made. Stop the NVR and put the original key file back (see docs/database-recovery.md); until "
                     "then cameras cannot be reached and settings that hold a secret cannot be saved.", path)


def missing() -> bool:
    """Whether the key is needed and not there: for the start-up check and its incident."""
    return not key_path().exists() and _stored_secrets_exist()


def report_at_start() -> None:
    """Raise the incident while the key is missing, and clear it once it is back. Start-up goes on either way: the footage already on
    disk can still be watched, and the incident is where an operator will see what is wrong."""
    from .incidents import sync_incident
    from .store import connect

    absent = missing()
    if absent:
        _say_once(key_path())
    with connect() as db:
        if absent or db.execute("SELECT 1 FROM incidents WHERE incident_key='credential_key' AND fault_active=1").fetchone():
            sync_incident(db, incident_key="credential_key", camera_id=None, fault_type="credential_key", active=absent,
                          summary="The credential key file is missing. Stored camera passwords and addresses cannot be read until the original key file is put back.")


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
