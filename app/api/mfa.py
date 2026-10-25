"""Second factors: the sign-in steps after the password, enrolment from the account menu, and administrator controls."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app import auth_limits as limits, mfa, webauthn
from app.api.identity import signed_in
from app.audit import record
from app.principal import COOKIE_NAME, Administrator, CurrentUser, Principal
from app.security import token_digest, verify_password
from app.store import connect

router = APIRouter()

REGISTRATION_MINUTES = 5
NO_FACTOR_FOR_ENTRA = "This account signs in with Microsoft Entra; its second factor is managed by your organization."


def _origin(request: Request) -> str:
    return request.headers.get("origin") or f"{request.url.scheme}://{request.headers.get('host', '')}"


def _relying_party(request: Request) -> webauthn.RelyingParty:
    try:
        return webauthn.relying_party(_origin(request))
    except webauthn.WebAuthnError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _expired() -> HTTPException:
    return HTTPException(status_code=401, detail="This sign-in has expired. Start again.")


# --- Sign-in: the second step -----------------------------------------------------------

class SecondFactorRequest(BaseModel):
    challenge: str = Field(min_length=1, max_length=128)
    method: Literal["totp", "recovery"]
    code: str = Field(min_length=1, max_length=64)


class ChallengeRequest(BaseModel):
    challenge: str = Field(min_length=1, max_length=128)


class PasskeyAssertion(BaseModel):
    challenge: str = Field(min_length=1, max_length=128)
    credential: dict[str, Any]


def _open_challenge(raw: str, kind: str = "login", *, claim: bool = False) -> tuple[sqlite3.Row, sqlite3.Row | None]:
    """The sign-in a challenge belongs to. ``claim`` spends one of its attempts first, atomically, for steps that judge an answer."""
    with connect() as db:
        challenge = mfa.claim_attempt(db, raw, kind) if claim else mfa.get_challenge(db, raw, kind)
        if challenge is None:
            raise _expired()
        user = db.execute("SELECT id,username,role,provider_issuer FROM users WHERE id=?", (challenge["user_id"],)).fetchone() if challenge["user_id"] else None
    return challenge, user


def _consume(db: sqlite3.Connection, raw: str) -> None:
    """Use the challenge up. Of several answers racing for one challenge, only one gets a session."""
    if not mfa.burn_challenge(db, raw):
        raise _expired()


def _failed(user_id: int | None, username: str, address: str, method: str, reason: str) -> None:
    """Audit a wrong answer. The attempt was already counted against the throttle when it began."""
    with connect() as db:
        record(db, user_id, "mfa_failed", "user", user_id if user_id is not None else username, method=method, reason=reason, address=address)


@router.post("/api/auth/mfa/verify")
def verify_second_factor(payload: SecondFactorRequest, request: Request, response: Response) -> dict[str, object]:
    """Answer a sign-in's challenge with an authenticator code, or with a recovery code (each works once)."""
    _, user = _open_challenge(payload.challenge, claim=True)
    assert user is not None
    address, account = limits.client_address(request), limits.account_key(user["username"])
    limits.begin(account, address)
    user_id = int(user["id"])
    with connect() as db:
        if payload.method == "totp":
            stored = mfa.stored_totp(db, user_id, confirmed=True)
            step = mfa.matching_totp_step(stored[0], payload.code, stored[1]) if stored else None
            # Only the first use of a time step counts, even for two requests at once.
            accepted = step is not None and db.execute("UPDATE user_totp SET last_step=? WHERE user_id=? AND last_step<?", (step, user_id, step)).rowcount == 1
        else:
            accepted = mfa.use_recovery_code(db, user_id, payload.code)
    if not accepted:
        _failed(user_id, user["username"], address, payload.method, "wrong_code")
        raise HTTPException(status_code=401, detail="That code is not right")
    with connect() as db:
        _consume(db, payload.challenge)
        record(db, user_id, "mfa_recovery_code_used" if payload.method == "recovery" else "mfa_used", "user", user_id, method=payload.method, address=address)
    return signed_in(response, user, account, address)


def _passkeys_of(db: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    return [{"credential_id": row["credential_id"], "transports": json.loads(row["transports_json"])}
            for row in db.execute("SELECT credential_id,transports_json FROM user_passkeys WHERE user_id=? ORDER BY id", (user_id,))]


def _store_challenge(db: sqlite3.Connection, raw: str, value: bytes) -> None:
    db.execute("UPDATE mfa_challenges SET webauthn_challenge=? WHERE token_hash=?", (webauthn.b64url(value), mfa.token_digest(raw)))


@router.post("/api/auth/mfa/passkey/options")
def second_factor_passkey_options(payload: ChallengeRequest, request: Request) -> dict[str, Any]:
    _, user = _open_challenge(payload.challenge)
    assert user is not None
    value = webauthn.new_challenge()
    with connect() as db:
        allowed = _passkeys_of(db, int(user["id"]))
        _store_challenge(db, payload.challenge, value)
    if not allowed:
        raise HTTPException(status_code=409, detail="This account has no passkey")
    return webauthn.request_options(_relying_party(request), value, allowed, "preferred")


def _check_assertion(db: sqlite3.Connection, credential: dict[str, Any], webauthn_challenge: str | None, request: Request,
                     user_id: int | None, require_verified: bool) -> sqlite3.Row:
    """The stored passkey this assertion proves, or a refusal. ``user_id`` limits it to one person's passkeys."""
    if not webauthn_challenge:
        raise HTTPException(status_code=409, detail="Ask for passkey options first")
    row = db.execute("SELECT * FROM user_passkeys WHERE credential_id=?", (str(credential.get("id", "")),)).fetchone()
    if row is None or (user_id is not None and row["user_id"] != user_id):
        raise webauthn.WebAuthnError("This passkey is not registered for this account")
    count = webauthn.verify_assertion(credential, webauthn.unb64url(webauthn_challenge), _relying_party(request), bytes(row["public_key"]),
                                      int(row["sign_count"]), require_verified)
    swapped = db.execute("UPDATE user_passkeys SET sign_count=?,last_used_at=? WHERE id=? AND sign_count=?",
                         (count, datetime.now(timezone.utc).isoformat(), row["id"], row["sign_count"]))
    if swapped.rowcount != 1:
        raise webauthn.WebAuthnError("This passkey was just used for another sign-in")
    return row


@router.post("/api/auth/mfa/passkey/verify")
def verify_passkey_second_factor(payload: PasskeyAssertion, request: Request, response: Response) -> dict[str, object]:
    challenge, user = _open_challenge(payload.challenge, claim=True)
    assert user is not None
    address, account = limits.client_address(request), limits.account_key(user["username"])
    limits.begin(account, address)
    user_id = int(user["id"])
    try:
        with connect() as db:
            _check_assertion(db, payload.credential, challenge["webauthn_challenge"], request, user_id, require_verified=False)
            _consume(db, payload.challenge)
    except webauthn.WebAuthnError as exc:
        _failed(user_id, user["username"], address, "passkey", str(exc))
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    with connect() as db:
        record(db, user_id, "mfa_used", "user", user_id, method="passkey", address=address)
    return signed_in(response, user, account, address)


# --- Sign-in: a passkey alone -----------------------------------------------------------

MAX_PENDING_PASSKEY_SIGN_INS = 1000


@router.post("/api/auth/passkey/options")
def passkey_sign_in_options(request: Request) -> dict[str, Any]:
    """Options for signing in with only a passkey, for people who turned that on."""
    value = webauthn.new_challenge()
    with connect() as db:
        pending = db.execute("SELECT COUNT(*) FROM mfa_challenges WHERE kind='passkey_login' AND expires_at>?", (datetime.now(timezone.utc).isoformat(),)).fetchone()[0]
        if pending >= MAX_PENDING_PASSKEY_SIGN_INS:
            raise HTTPException(status_code=429, detail="Too many sign-ins are waiting. Try again in a few minutes.", headers={"Retry-After": "60"})
        raw = mfa.new_challenge(db, "passkey_login", None)
        _store_challenge(db, raw, value)
    return {"challenge": raw, "options": webauthn.request_options(_relying_party(request), value, [], "required")}


@router.post("/api/auth/passkey/login")
def passkey_sign_in(payload: PasskeyAssertion, request: Request, response: Response) -> dict[str, object]:
    challenge, _ = _open_challenge(payload.challenge, "passkey_login", claim=True)
    address = limits.client_address(request)
    limits.begin("", address)  # Counts the address; the account is counted once the passkey says whose it is.
    try:
        with connect() as db:
            row = db.execute("SELECT user_id FROM user_passkeys WHERE credential_id=?", (str(payload.credential.get("id", "")),)).fetchone()
            user = db.execute("SELECT id,username,role,provider_issuer,passkey_sign_in FROM users WHERE id=?", (row["user_id"],)).fetchone() if row else None
        if user is None:
            raise webauthn.WebAuthnError("This passkey is not registered")
        account = limits.account_key(user["username"])
        limits.begin_account(account, address)
        if not user["passkey_sign_in"]:
            raise webauthn.WebAuthnError("Signing in with only a passkey is turned off for this account")
        with connect() as db:
            _check_assertion(db, payload.credential, challenge["webauthn_challenge"], request, int(user["id"]), require_verified=True)
            _consume(db, payload.challenge)
    except webauthn.WebAuthnError as exc:
        _failed(int(user["id"]) if user else None, user["username"] if user else "", address, "passkey_sign_in", str(exc))
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    with connect() as db:
        record(db, int(user["id"]), "mfa_used", "user", int(user["id"]), method="passkey_sign_in", address=address)
    return signed_in(response, user, account, address)


# --- Enrolment, from the account menu ---------------------------------------------------

def _local_account(user: Principal) -> sqlite3.Row:
    if user.scopes is not None:
        # An API token acts for its creator but is not a person at a keyboard: sign-in is changed from a browser session.
        raise HTTPException(status_code=403, detail="Change sign-in security from a browser session, not an API token")
    with connect() as db:
        row = db.execute("SELECT id,username,role,provider_issuer,password_hash,passkey_sign_in FROM users WHERE id=?", (user.id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="Session expired")
    if row["provider_issuer"] is not None:
        raise HTTPException(status_code=409, detail=NO_FACTOR_FOR_ENTRA)
    return row


def _confirm_password(account: sqlite3.Row, password: str, request: Request) -> None:
    """Changing how someone signs in needs their password again, so a borrowed session cannot strip a factor."""
    key, address = limits.account_key(account["username"]), limits.client_address(request)
    limits.begin(key, address)
    if not verify_password(password, account["password_hash"]):
        with connect() as db:
            record(db, int(account["id"]), "mfa_failed", "user", int(account["id"]), method="password_confirmation", reason="wrong_password", address=address)
        raise HTTPException(status_code=403, detail="That password is not right")
    limits.release(key, address)


def _confirm_password_to_add(account: sqlite3.Row, password: str | None, request: Request) -> None:
    """Adding a factor to an account that already has one needs the password too, or a borrowed session could add its own."""
    with connect() as db:
        protected = mfa.has_factor(db, int(account["id"]))
    if protected:
        if not password:
            raise HTTPException(status_code=403, detail="Confirm your password to add another factor")
        _confirm_password(account, password, request)


def _end_other_sessions(db: sqlite3.Connection, user_id: int, request: Request) -> None:
    """After a change to how someone signs in, every other session of theirs ends; the one making the change stays."""
    raw = request.cookies.get(COOKIE_NAME)
    db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash!=?", (user_id, token_digest(raw) if raw else ""))


class PasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class OptionalPassword(BaseModel):
    password: str | None = Field(default=None, max_length=1024)


class TotpConfirmRequest(BaseModel):
    code: str = Field(min_length=1, max_length=16)


class PasskeyRegistration(BaseModel):
    credential: dict[str, Any]
    name: str = Field(default="Passkey", max_length=64)


class PasskeySignInRequest(BaseModel):
    enabled: bool
    password: str = Field(min_length=1, max_length=1024)


def _summary(user: Principal) -> dict[str, object]:
    with connect() as db:
        row = db.execute("SELECT provider_issuer,passkey_sign_in FROM users WHERE id=?", (user.id,)).fetchone()
        policy = mfa.policy(db)
        found = mfa.factors(db, user.id)
    local = row is not None and row["provider_issuer"] is None
    return {
        "local": local,
        "policy": policy,
        "required": mfa.required_for(policy, user.role, local),
        "totp": found["totp"],
        "passkeys": [{key: item[key] for key in ("id", "name", "created_at", "last_used_at")} for item in found["passkeys"]],
        "recovery_codes_remaining": found["recovery_codes_remaining"],
        "passkey_sign_in": bool(row and row["passkey_sign_in"]),
    }


@router.get("/api/me/security")
def security_summary(user: CurrentUser) -> dict[str, object]:
    return _summary(user)


def _first_codes(db: sqlite3.Connection, user_id: int) -> list[str] | None:
    """Recovery codes, made when the first factor is set up so there is a way back in; None when there already are some."""
    if db.execute("SELECT COUNT(*) FROM user_recovery_codes WHERE user_id=? AND used_at IS NULL", (user_id,)).fetchone()[0]:
        return None
    codes = mfa.new_recovery_codes(db, user_id)
    record(db, user_id, "mfa_recovery_codes_generated", "user", user_id, reason="first_factor")
    return codes


@router.post("/api/me/security/totp/begin")
def begin_totp(request: Request, user: CurrentUser, payload: OptionalPassword | None = None) -> dict[str, str]:
    account = _local_account(user)
    _confirm_password_to_add(account, payload.password if payload else None, request)
    with connect() as db:
        if mfa.stored_totp(db, user.id, confirmed=True):
            raise HTTPException(status_code=409, detail="An authenticator app is already set up; remove it first to set up another")
        secret = mfa.new_totp_secret()
        mfa.save_totp_secret(db, user.id, secret)
    return {"secret": mfa.secret_text(secret), "otpauth_uri": mfa.otpauth_uri(secret, account["username"])}


@router.post("/api/me/security/totp/confirm")
def confirm_totp(payload: TotpConfirmRequest, request: Request, user: CurrentUser) -> dict[str, object]:
    account = _local_account(user)
    key, address = limits.account_key(account["username"]), limits.client_address(request)
    limits.begin(key, address)
    with connect() as db:
        stored = mfa.stored_totp(db, user.id, confirmed=False)
        if stored is None:
            raise HTTPException(status_code=409, detail="Start setting up the authenticator app first")
        step = mfa.matching_totp_step(stored[0], payload.code, 0)
        if step is None:
            record(db, user.id, "mfa_failed", "user", user.id, method="totp_enrolment", reason="wrong_code", address=address)
            raise HTTPException(status_code=422, detail="That code is not right. Check the app's clock and try the next code.")
        db.execute("UPDATE user_totp SET confirmed_at=?,last_step=? WHERE user_id=?", (datetime.now(timezone.utc).isoformat(), step, user.id))
        limits.release(key, address)
        record(db, user.id, "mfa_totp_enrolled", "user", user.id)
        codes = _first_codes(db, user.id)
    return {"recovery_codes": codes, **_summary(user)}


@router.delete("/api/me/security/totp")
def remove_totp(payload: PasswordRequest, request: Request, user: CurrentUser) -> dict[str, object]:
    account = _local_account(user)
    _confirm_password(account, payload.password, request)
    with connect() as db:
        db.execute("DELETE FROM user_totp WHERE user_id=?", (user.id,))
        _after_removal(db, user.id, "totp")
        _end_other_sessions(db, user.id, request)
    return _summary(user)


def _after_removal(db: sqlite3.Connection, user_id: int, factor: str) -> None:
    record(db, user_id, "mfa_factor_removed", "user", user_id, factor=factor)
    if not mfa.has_factor(db, user_id):
        # Recovery codes and passkey-only sign-in mean nothing without a factor.
        db.execute("DELETE FROM user_recovery_codes WHERE user_id=?", (user_id,))
        db.execute("UPDATE users SET passkey_sign_in=0 WHERE id=?", (user_id,))
    elif not db.execute("SELECT 1 FROM user_passkeys WHERE user_id=?", (user_id,)).fetchone():
        db.execute("UPDATE users SET passkey_sign_in=0 WHERE id=?", (user_id,))


@router.post("/api/me/security/passkeys/options")
def passkey_creation_options(request: Request, user: CurrentUser, payload: OptionalPassword | None = None) -> dict[str, Any]:
    account = _local_account(user)
    _confirm_password_to_add(account, payload.password if payload else None, request)
    value = webauthn.new_challenge()
    with connect() as db:
        existing = [item["credential_id"] for item in _passkeys_of(db, user.id)]
        db.execute("UPDATE users SET passkey_registration_challenge=?,passkey_registration_expires_at=? WHERE id=?",
                   (webauthn.b64url(value), (datetime.now(timezone.utc) + timedelta(minutes=REGISTRATION_MINUTES)).isoformat(), user.id))
    return webauthn.creation_options(_relying_party(request), value, user.id, account["username"], existing)


@router.post("/api/me/security/passkeys", status_code=status.HTTP_201_CREATED)
def register_passkey(payload: PasskeyRegistration, request: Request, user: CurrentUser) -> dict[str, object]:
    _local_account(user)
    with connect() as db:
        row = db.execute("SELECT passkey_registration_challenge,passkey_registration_expires_at FROM users WHERE id=?", (user.id,)).fetchone()
        db.execute("UPDATE users SET passkey_registration_challenge=NULL,passkey_registration_expires_at=NULL WHERE id=?", (user.id,))  # Single use.
    if not row["passkey_registration_challenge"] or row["passkey_registration_expires_at"] <= datetime.now(timezone.utc).isoformat():
        raise HTTPException(status_code=409, detail="Start adding the passkey again")
    address = limits.client_address(request)
    try:
        passkey = webauthn.verify_registration(payload.credential, webauthn.unb64url(row["passkey_registration_challenge"]), _relying_party(request))
    except webauthn.WebAuthnError as exc:
        with connect() as db:
            record(db, user.id, "mfa_failed", "user", user.id, method="passkey_enrolment", reason=str(exc), address=address)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with connect() as db:
        try:
            db.execute("INSERT INTO user_passkeys(user_id,credential_id,public_key,algorithm,sign_count,transports_json,name) VALUES(?,?,?,?,?,?,?)",
                       (user.id, passkey.credential_id, passkey.public_key, passkey.algorithm, passkey.sign_count, json.dumps(passkey.transports),
                        payload.name.strip() or "Passkey"))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="This passkey is already registered") from exc
        record(db, user.id, "mfa_passkey_enrolled", "user", user.id, name=payload.name.strip() or "Passkey")
        codes = _first_codes(db, user.id)
    return {"recovery_codes": codes, **_summary(user)}


@router.delete("/api/me/security/passkeys/{passkey_id}")
def remove_passkey(passkey_id: int, payload: PasswordRequest, request: Request, user: CurrentUser) -> dict[str, object]:
    account = _local_account(user)
    _confirm_password(account, payload.password, request)
    with connect() as db:
        if db.execute("DELETE FROM user_passkeys WHERE id=? AND user_id=?", (passkey_id, user.id)).rowcount == 0:
            raise HTTPException(status_code=404, detail="Passkey not found")
        _after_removal(db, user.id, "passkey")
        _end_other_sessions(db, user.id, request)
    return _summary(user)


@router.put("/api/me/security/passkey-sign-in")
def set_passkey_sign_in(payload: PasskeySignInRequest, request: Request, user: CurrentUser) -> dict[str, object]:
    account = _local_account(user)
    _confirm_password(account, payload.password, request)
    with connect() as db:
        if payload.enabled and not db.execute("SELECT 1 FROM user_passkeys WHERE user_id=?", (user.id,)).fetchone():
            raise HTTPException(status_code=409, detail="Add a passkey first")
        db.execute("UPDATE users SET passkey_sign_in=? WHERE id=?", (int(payload.enabled), user.id))
        record(db, user.id, "mfa_passkey_sign_in_changed", "user", user.id, enabled=payload.enabled)
        _end_other_sessions(db, user.id, request)
    return _summary(user)


@router.post("/api/me/security/recovery-codes")
def regenerate_recovery_codes(payload: PasswordRequest, request: Request, user: CurrentUser) -> dict[str, object]:
    """Ten new recovery codes, replacing the old ones. They are shown once."""
    account = _local_account(user)
    _confirm_password(account, payload.password, request)
    with connect() as db:
        if not mfa.has_factor(db, user.id):
            raise HTTPException(status_code=409, detail="Set up a second factor first")
        codes = mfa.new_recovery_codes(db, user.id)
        record(db, user.id, "mfa_recovery_codes_generated", "user", user.id, reason="regenerated")
        _end_other_sessions(db, user.id, request)
    return {"recovery_codes": codes, **_summary(user)}


# --- Administrators ---------------------------------------------------------------------

class PolicyRequest(BaseModel):
    mfa_policy: Literal["off", "administrators", "all"]


@router.get("/api/admin/security")
def get_security_policy(_: Administrator) -> dict[str, str]:
    with connect() as db:
        return {"mfa_policy": mfa.policy(db)}


@router.put("/api/admin/security")
def put_security_policy(payload: PolicyRequest, admin: Administrator) -> dict[str, str]:
    with connect() as db:
        before = mfa.policy(db)
        db.execute("UPDATE security_settings SET mfa_policy=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE singleton_id=1", (payload.mfa_policy,))
        if before != payload.mfa_policy:
            record(db, admin.id, "mfa_policy_changed", "security_settings", 1, before=before, after=payload.mfa_policy)
    return {"mfa_policy": payload.mfa_policy}


@router.get("/api/admin/users")
def list_users(_: Administrator) -> dict[str, object]:
    with connect() as db:
        rows = db.execute("SELECT id,username,role,provider_issuer,passkey_sign_in,created_at FROM users ORDER BY username COLLATE NOCASE").fetchall()
        users = []
        for row in rows:
            found = mfa.factors(db, int(row["id"]))
            users.append({"id": row["id"], "username": row["username"], "role": row["role"], "local": row["provider_issuer"] is None,
                          "totp": found["totp"], "passkeys": len(found["passkeys"]), "recovery_codes_remaining": found["recovery_codes_remaining"],
                          "passkey_sign_in": bool(row["passkey_sign_in"])})
    return {"users": users}


@router.delete("/api/admin/users/{user_id}/mfa", status_code=status.HTTP_204_NO_CONTENT)
def reset_user_factors(user_id: int, admin: Administrator) -> Response:
    """Remove every second factor of someone who lost theirs, and end their sessions. Audited."""
    if user_id == admin.id:
        raise HTTPException(status_code=409, detail="Manage your own second factors from the account menu")
    with connect() as db:
        target = db.execute("SELECT id,username FROM users WHERE id=?", (user_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")
        before = mfa.factors(db, user_id)
        mfa.reset_factors(db, user_id)
        db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        record(db, admin.id, "mfa_reset", "user", user_id, username=target["username"], had_totp=before["totp"], passkeys=len(before["passkeys"]))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
