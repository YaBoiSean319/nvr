"""First-run setup, local and Entra sign-in, and session administration."""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from authlib.integrations.starlette_client import OAuth

from app.principal import secure_cookie, COOKIE_NAME, Administrator, CurrentUser
from app.security import hash_password, new_session_token, token_digest, verify_password
from app.store import connect

router = APIRouter()


USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.@-]{3,128}$")


oauth = OAuth()


def _oidc_settings() -> dict[str, str] | None:
    required = ("NVR_PUBLIC_URL", "NVR_OIDC_TENANT_ID", "NVR_OIDC_CLIENT_ID", "NVR_OIDC_CLIENT_SECRET", "NVR_OIDC_SESSION_SECRET")
    values = {key: os.environ.get(key, "").strip() for key in required}
    if not all(values.values()) or len(values["NVR_OIDC_SESSION_SECRET"]) < 32:
        return None
    if not any(os.environ.get(key, "").strip() for key in ("NVR_OIDC_ADMIN_ROLES", "NVR_OIDC_OPERATOR_ROLES")):
        return None
    public_url = values["NVR_PUBLIC_URL"].rstrip("/")
    parsed_url = urlsplit(public_url)
    if (parsed_url.scheme != "https" and not (parsed_url.scheme == "http" and parsed_url.hostname in {"localhost", "127.0.0.1"})) or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.path or parsed_url.query or parsed_url.fragment:
        return None
    try:
        if str(uuid.UUID(values["NVR_OIDC_TENANT_ID"])).casefold() != values["NVR_OIDC_TENANT_ID"].casefold():
            return None
    except ValueError:
        return None
    return {
        "public_url": public_url,
        "tenant_id": values["NVR_OIDC_TENANT_ID"],
        "client_id": values["NVR_OIDC_CLIENT_ID"],
        "client_secret": values["NVR_OIDC_CLIENT_SECRET"],
        "discovery_url": f"https://login.microsoftonline.com/{values['NVR_OIDC_TENANT_ID']}/v2.0/.well-known/openid-configuration",
    }


def _oidc_client():
    config = _oidc_settings()
    if config is None:
        return None
    return oauth.register(
        name="entra",
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        server_metadata_url=config["discovery_url"],
        client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256", "trust_env": False},
    )


def _mapped_role(claims: dict[str, object]) -> str | None:
    roles = claims.get("roles", [])
    if isinstance(roles, str):
        roles = [roles]
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        return None
    admin_roles = {x.strip() for x in os.environ.get("NVR_OIDC_ADMIN_ROLES", "").split(",") if x.strip()}
    operator_roles = {x.strip() for x in os.environ.get("NVR_OIDC_OPERATOR_ROLES", "").split(",") if x.strip()}
    assigned = set(roles)
    if assigned & admin_roles:
        return "administrator"
    if assigned & operator_roles:
        return "operator"
    return None


class BootstrapRequest(BaseModel):
    username: str = Field(min_length=3, max_length=128)
    password: str = Field(min_length=14, max_length=1024)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


def _session_hours() -> int:
    try:
        return min(max(int(os.environ.get("NVR_SESSION_HOURS", "12")), 1), 168)
    except ValueError:
        return 12


def _new_session(response: Response, user_id: int) -> None:
    raw = new_session_token()
    session_id = str(uuid.uuid4())
    expires = datetime.now(timezone.utc) + timedelta(hours=_session_hours())
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (datetime.now(timezone.utc).isoformat(),))
        db.execute(
            "INSERT INTO sessions(session_id, token_hash, user_id, expires_at) VALUES (?, ?, ?, ?)",
            (session_id, token_digest(raw), user_id, expires.isoformat()),
        )
    response.set_cookie(
        COOKIE_NAME,
        raw,
        httponly=True,
        secure=secure_cookie(),
        samesite="strict",
        path="/",
        max_age=_session_hours() * 3600,
    )


@router.get("/api/setup/status")
def setup_status() -> dict[str, bool]:
    with connect() as db:
        configured = db.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0] == 1
    return {"setup_required": not configured}


@router.post("/api/setup/admin", status_code=status.HTTP_201_CREATED)
def bootstrap(payload: BootstrapRequest, response: Response) -> dict[str, str]:
    username = payload.username.strip()
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(status_code=422, detail="Use 3–128 letters, numbers, or . _ @ - characters")
    if len(payload.password) < 14 or not any(c.isalpha() for c in payload.password) or not any(c.isdigit() for c in payload.password):
        raise HTTPException(status_code=422, detail="Password must be at least 14 characters and include a letter and a number")
    with connect() as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0]:
                db.rollback()
                raise HTTPException(status_code=409, detail="Initial setup is already complete")
            cur = db.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (?, ?, 'administrator')",
                (username, hash_password(payload.password)),
            )
            user_id = int(cur.lastrowid)
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="Initial setup is already complete") from exc
    _new_session(response, user_id)
    return {"username": username, "role": "administrator"}


@router.post("/api/auth/login")
def login(payload: LoginRequest, response: Response) -> dict[str, str]:
    with connect() as db:
        user = db.execute(
            "SELECT id, username, password_hash, role FROM users WHERE username=? COLLATE NOCASE",
            (payload.username.strip(),),
        ).fetchone()
    if user is None or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _new_session(response, user["id"])
    return {"username": user["username"], "role": user["role"]}


@router.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response) -> Response:
    raw = request.cookies.get(COOKIE_NAME)
    if raw:
        with connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(raw),))
    request.session.clear()
    response.delete_cookie(COOKIE_NAME, path="/", secure=secure_cookie(), httponly=True, samesite="strict")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/api/auth/me")
def me(user: CurrentUser) -> dict[str, str]:
    return {"username": user["username"], "role": user["role"]}


@router.get("/api/auth/oidc/status")
def oidc_status() -> dict[str, bool]:
    return {"enabled": _oidc_settings() is not None}


@router.get("/api/auth/oidc/login", include_in_schema=False, response_model=None)
async def oidc_login(request: Request) -> dict[str, str] | RedirectResponse:
    config = _oidc_settings()
    client = _oidc_client()
    if config is None or client is None:
        raise HTTPException(status_code=503, detail="Entra sign-in is not configured; use the emergency local administrator account")
    try:
        redirect_uri = config["public_url"] + "/api/auth/oidc/callback"
        if request.query_params.get("format") == "json":
            authorize_data = await client.create_authorization_url(redirect_uri)
            await client.save_authorize_data(request, redirect_uri=redirect_uri, **authorize_data)
            return {"authorization_url": authorize_data["url"]}
        return await client.authorize_redirect(request, redirect_uri)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Entra sign-in is temporarily unavailable; local administrator sign-in remains available") from exc


@router.get("/api/auth/oidc/callback", include_in_schema=False)
async def oidc_callback(request: Request) -> RedirectResponse:
    config = _oidc_settings()
    client = _oidc_client()
    if config is None or client is None:
        raise HTTPException(status_code=503, detail="Entra sign-in is not configured; use the emergency local administrator account")
    if request.query_params.get("error"):
        raise HTTPException(status_code=503, detail="Entra sign-in did not complete; local administrator sign-in remains available")
    try:
        token = await client.authorize_access_token(request)
        claims = token.get("userinfo")
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Entra sign-in could not be validated") from exc
    if not isinstance(claims, dict) or not isinstance(claims.get("sub"), str) or not claims.get("sub"):
        raise HTTPException(status_code=401, detail="Entra sign-in did not include a valid identity")
    tenant_id = config["tenant_id"].casefold()
    if str(claims.get("tid", "")).casefold() != tenant_id:
        raise HTTPException(status_code=403, detail="This Entra tenant is not allowed")
    role = _mapped_role(claims)
    if role is None:
        raise HTTPException(status_code=403, detail="Your Entra account has no NVR role assignment")
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or not issuer.startswith("https://login.microsoftonline.com/"):
        raise HTTPException(status_code=401, detail="Entra sign-in issuer is invalid")
    subject = claims["sub"]
    display = claims.get("preferred_username")
    username = display.strip()[:128] if isinstance(display, str) and display.strip() else f"entra-{subject[:112]}"
    with connect() as db:
        user = db.execute("SELECT id, username FROM users WHERE provider_issuer=? AND provider_subject=?", (issuer, subject)).fetchone()
        if user is None:
            if db.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone():
                username = f"entra-{subject[:112]}"
            try:
                cur = db.execute(
                    "INSERT INTO users(username,password_hash,role,provider_issuer,provider_subject) VALUES (?,?,?,?,?)",
                    (username, hash_password(secrets.token_urlsafe(48)), role, issuer, subject),
                )
                user_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=403, detail="Unable to provision this Entra identity") from exc
        else:
            user_id = int(user["id"])
            db.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
    redirect = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _new_session(redirect, user_id)
    return redirect


@router.get("/api/admin/sessions")
def list_sessions(_: Administrator) -> dict[str, object]:
    with connect() as db:
        rows = db.execute(
            "SELECT s.session_id,s.created_at,s.expires_at,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.expires_at>? ORDER BY s.created_at DESC", (datetime.now(timezone.utc).isoformat(),)
        ).fetchall()
    return {"sessions": [dict(row) for row in rows]}


@router.delete("/api/admin/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def terminate_session(session_id: str, _: Administrator) -> Response:
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
