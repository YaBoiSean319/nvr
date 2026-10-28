"""Second factors for local accounts: TOTP, recovery codes, passkey bookkeeping and the policy.

Entra accounts are never asked for a second factor here: their MFA stays with the tenant.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import struct
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from .credentials import decrypt, encrypt
from .security import token_digest

POLICIES = ("off", "administrators", "all")
TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1  # Accept the code before and after the current one, for clock drift.
RECOVERY_CODES = 10
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
CHALLENGE_MINUTES = 5
MAX_CHALLENGE_ATTEMPTS = 5
ISSUER = "Workplace NVR"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- TOTP (RFC 6238, SHA-1, six digits, thirty seconds) ---------------------------------

def new_totp_secret() -> bytes:
    return secrets.token_bytes(20)


def totp_code(secret: bytes, step: int) -> str:
    digest = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** TOTP_DIGITS
    return str(value).zfill(TOTP_DIGITS)


def totp_step(now: datetime | None = None) -> int:
    return int((now or _now()).timestamp()) // TOTP_STEP_SECONDS


def matching_totp_step(secret: bytes, code: str, last_step: int, now: datetime | None = None) -> int | None:
    """The time step ``code`` is valid for, if it is one not used before; None otherwise."""
    code = "".join(code.split())
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    current = totp_step(now)
    for step in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        if step > last_step and hmac.compare_digest(totp_code(secret, step), code):
            return step
    return None


def otpauth_uri(secret: bytes, username: str) -> str:
    encoded = base64.b32encode(secret).decode().rstrip("=")
    return (f"otpauth://totp/{quote(ISSUER)}:{quote(username)}?secret={encoded}&issuer={quote(ISSUER)}"
            f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}")


def secret_text(secret: bytes) -> str:
    """The secret as groups of four, for typing into an authenticator app."""
    encoded = base64.b32encode(secret).decode().rstrip("=")
    return " ".join(encoded[index:index + 4] for index in range(0, len(encoded), 4))


def stored_totp(db: sqlite3.Connection, user_id: int, confirmed: bool | None = None) -> tuple[bytes, int] | None:
    row = db.execute("SELECT secret_ciphertext,last_step,confirmed_at FROM user_totp WHERE user_id=?", (user_id,)).fetchone()
    if row is None or (confirmed is not None and bool(row["confirmed_at"]) != confirmed):
        return None
    return base64.b32decode(decrypt(row["secret_ciphertext"])), int(row["last_step"])


def save_totp_secret(db: sqlite3.Connection, user_id: int, secret: bytes) -> None:
    db.execute("INSERT INTO user_totp(user_id,secret_ciphertext) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET "
               "secret_ciphertext=excluded.secret_ciphertext,confirmed_at=NULL,last_step=0",
               (user_id, encrypt(base64.b32encode(secret).decode())))


# --- Recovery codes ---------------------------------------------------------------------

def _code_digest(code: str) -> str:
    normalised = "".join(character for character in code.lower() if character.isalnum())
    return hashlib.sha256(("nvr-recovery:" + normalised).encode()).hexdigest()


def new_recovery_codes(db: sqlite3.Connection, user_id: int) -> list[str]:
    """Ten fresh one-time codes, replacing every earlier one. They are shown once and kept only as digests."""
    codes = ["".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(10)) for _ in range(RECOVERY_CODES)]
    db.execute("DELETE FROM user_recovery_codes WHERE user_id=?", (user_id,))
    db.executemany("INSERT INTO user_recovery_codes(user_id,code_hash) VALUES(?,?)", [(user_id, _code_digest(code)) for code in codes])
    return [f"{code[:5]}-{code[5:]}" for code in codes]


def use_recovery_code(db: sqlite3.Connection, user_id: int, code: str) -> bool:
    """Spend a recovery code: true once for each code, never again."""
    spent = db.execute("UPDATE user_recovery_codes SET used_at=? WHERE user_id=? AND code_hash=? AND used_at IS NULL",
                       (_now().isoformat(), user_id, _code_digest(code)))
    return spent.rowcount == 1


# --- Factors and policy -----------------------------------------------------------------

def factors(db: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    totp = db.execute("SELECT 1 FROM user_totp WHERE user_id=? AND confirmed_at IS NOT NULL", (user_id,)).fetchone() is not None
    passkeys = [dict(row) for row in db.execute(
        "SELECT id,name,created_at,last_used_at,credential_id,transports_json FROM user_passkeys WHERE user_id=? ORDER BY id", (user_id,))]
    remaining = db.execute("SELECT COUNT(*) FROM user_recovery_codes WHERE user_id=? AND used_at IS NULL", (user_id,)).fetchone()[0]
    return {"totp": totp, "passkeys": passkeys, "recovery_codes_remaining": int(remaining)}


def has_factor(db: sqlite3.Connection, user_id: int) -> bool:
    found = factors(db, user_id)
    return found["totp"] or bool(found["passkeys"])


def policy(db: sqlite3.Connection) -> str:
    return db.execute("SELECT mfa_policy FROM security_settings WHERE singleton_id=1").fetchone()["mfa_policy"]


def remote_required(db: sqlite3.Connection) -> bool:
    """Whether sessions from outside the local network need a second factor (#132)."""
    return bool(db.execute("SELECT remote_mfa FROM security_settings WHERE singleton_id=1").fetchone()["remote_mfa"])


def required_for(policy_value: str, role: str, local: bool, remote_policy: bool = False, remote: bool = False) -> bool:
    """Whether the policy asks this account for a second factor (Entra accounts never are): by role, or because the session is remote and remote sessions need one."""
    return local and (policy_value == "all" or (policy_value == "administrators" and role == "administrator") or (remote_policy and remote))


def reset_factors(db: sqlite3.Connection, user_id: int) -> None:
    for table in ("user_totp", "user_passkeys", "user_recovery_codes"):
        db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
    db.execute("UPDATE users SET passkey_sign_in=0,passkey_registration_challenge=NULL,passkey_registration_expires_at=NULL WHERE id=?", (user_id,))
    db.execute("DELETE FROM mfa_challenges WHERE user_id=?", (user_id,))


# --- Sign-in challenges -----------------------------------------------------------------

def new_challenge(db: sqlite3.Connection, kind: str, user_id: int | None) -> str:
    """The state between a sign-in's steps, handed to the browser as an opaque single-use token."""
    db.execute("DELETE FROM mfa_challenges WHERE expires_at<=?", (_now().isoformat(),))
    raw = secrets.token_urlsafe(32)
    db.execute("INSERT INTO mfa_challenges(token_hash,kind,user_id,expires_at) VALUES(?,?,?,?)",
               (token_digest(raw), kind, user_id, (_now() + timedelta(minutes=CHALLENGE_MINUTES)).isoformat()))
    return raw


def get_challenge(db: sqlite3.Connection, raw: str, kind: str) -> sqlite3.Row | None:
    """A challenge that is still open, without spending an attempt (for asking for passkey options)."""
    return db.execute("SELECT * FROM mfa_challenges WHERE token_hash=? AND kind=? AND expires_at>? AND attempts<?",
                      (token_digest(raw), kind, _now().isoformat(), MAX_CHALLENGE_ATTEMPTS)).fetchone()


def claim_attempt(db: sqlite3.Connection, raw: str, kind: str) -> sqlite3.Row | None:
    """Spend one of a challenge's attempts before an answer is judged, so parallel guesses share the cap
    instead of each seeing an attempt left."""
    spent = db.execute("UPDATE mfa_challenges SET attempts=attempts+1 WHERE token_hash=? AND kind=? AND expires_at>? AND attempts<?",
                       (token_digest(raw), kind, _now().isoformat(), MAX_CHALLENGE_ATTEMPTS))
    if spent.rowcount != 1:
        return None
    return db.execute("SELECT * FROM mfa_challenges WHERE token_hash=?", (token_digest(raw),)).fetchone()


def burn_challenge(db: sqlite3.Connection, raw: str) -> bool:
    """Use a challenge up. Only one caller can: a sign-in is accepted only by the one that gets True."""
    return db.execute("DELETE FROM mfa_challenges WHERE token_hash=?", (token_digest(raw),)).rowcount == 1


def main(argv: list[str] | None = None) -> int:
    """``python -m app.mfa reset USERNAME``: remove a person's second factors from the host.

    For an administrator who lost their authenticator and recovery codes when no other administrator can reset them.
    Whoever can run it can already read the database, so it needs no password; it is audited."""
    import argparse

    from .audit import record
    from .store import connect, initialize

    parser = argparse.ArgumentParser(prog="python -m app.mfa", description="Second factor administration from the NVR host")
    commands = parser.add_subparsers(dest="command", required=True)
    reset = commands.add_parser("reset", help="Remove every second factor of a user and end their sessions")
    reset.add_argument("username")
    arguments = parser.parse_args(argv)
    initialize()
    with connect() as db:
        user = db.execute("SELECT id,username FROM users WHERE username=? COLLATE NOCASE", (arguments.username,)).fetchone()
        if user is None:
            print(f"No user named {arguments.username!r}")
            return 1
        before = factors(db, int(user["id"]))
        reset_factors(db, int(user["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=?", (int(user["id"]),))
        db.execute("DELETE FROM device_sessions WHERE user_id=?", (int(user["id"]),))  # A phone's stored secret is a session too
        record(db, None, "mfa_reset", "user", int(user["id"]), username=user["username"], had_totp=before["totp"], passkeys=len(before["passkeys"]), via="host")
    print(f"Removed the second factors of {user['username']}; they will sign in with their password and set up a new one if the policy asks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
