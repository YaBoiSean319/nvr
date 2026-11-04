"""Send email through Microsoft 365 with a Microsoft sign-in, not a mail password (#231).

An administrator gives the tenant and the app registration (a public client with "Allow public client flows" on), the NVR asks Microsoft for a device sign-in
(a short code and a web address), the administrator signs in at that address, and the NVR keeps the refresh token it gets back, encrypted like camera
credentials. Mail goes out through Microsoft Graph ``sendMail`` with the one permission ``Mail.Send`` (delegated: it sends as the signed-in account, or as a
shared mailbox that account may send as).

Nothing here puts a token, a code or a secret in an error: failures are short codes (``MailError.code``) with a plain sentence for people.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from .credentials import decrypt, encrypt
from .store import connect

LOGIN_URL = os.environ.get("NVR_MS_LOGIN_URL", "https://login.microsoftonline.com").rstrip("/")
GRAPH_URL = os.environ.get("NVR_MS_GRAPH_URL", "https://graph.microsoft.com/v1.0").rstrip("/")
SCOPE = "openid profile offline_access https://graph.microsoft.com/Mail.Send"
REQUEST_TIMEOUT = 15
REFRESH_MARGIN = timedelta(minutes=2)
MAX_RECIPIENTS = 20
MAX_SUBJECT = 200
MAX_BODY = 20000

TENANT = re.compile(r"^(?:[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}|[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+)$")
CLIENT = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
ADDRESS = re.compile(r"^[^\s@<>,;\"]{1,64}@[^\s@<>,;\"]{1,255}\.[A-Za-z]{2,}$")

_lock = threading.Lock()  # One token refresh at a time: Microsoft rotates refresh tokens, so two at once would lose one

MESSAGES = {
    "authorization_declined": "Sign-in was declined. Start again.",
    "expired_token": "The sign-in code expired. Start again.",
    "bad_verification_code": "The sign-in code was not accepted. Start again.",
    "invalid_grant": "The Microsoft sign-in expired or was withdrawn. Reconnect.",
    "invalid_client": "Microsoft does not accept this app. Check the application (client) ID and that public client flows are allowed.",
    "unauthorized_client": "This app may not use the device sign-in. Turn on \"Allow public client flows\" for the app registration.",
    "invalid_request": "Microsoft refused the request. Check the tenant and the application ID.",
    "not_connected": "Email is not connected to Microsoft.",
    "unreachable": "Microsoft could not be reached.",
    "forbidden": "Microsoft refused to send as this account. It may not be allowed to send mail, or the shared mailbox may not allow it.",
    "rejected": "Microsoft rejected the message.",
    "throttled": "Microsoft asked us to slow down; will try again.",
    "unexpected": "Microsoft answered in a way that was not understood.",
}


# What Microsoft's own error codes usually mean, in words an administrator can act on (the code itself is always shown too).
HINTS = {
    "ErrorAccessDenied": "The signed-in account is not allowed to send mail here: it may have no Exchange Online mailbox or licence, or a policy blocks it.",
    "MailboxNotEnabledForRESTAPI": "The signed-in account has no Exchange Online mailbox for the API to use (it may be unlicensed, or its mailbox may be on-premises).",
    "MailboxNotFound": "The mailbox could not be found: check the account, or the shared mailbox in \"Send as\".",
    "ErrorSendAsDenied": "The signed-in account may not send as that shared mailbox: give it the Send As permission in Exchange.",
    "ErrorQuotaExceeded": "The mailbox is full or over its sending limit.",
    "ApplicationThrottled": "Microsoft is limiting how fast this app can send.",
    "ErrorInvalidRecipients": "Microsoft rejected one of the recipient addresses.",
    "AADSTS65001": "The account or an administrator has not consented to this app sending mail: sign in again and accept, or have an administrator grant consent for Mail.Send.",
    "AADSTS50076": "A second sign-in step (multi-factor) is required again: sign in again.",
    "AADSTS50158": "A conditional access policy needs more from this sign-in (for example a compliant device): it may block this app.",
    "AADSTS53003": "A conditional access policy blocked this app's access.",
    "AADSTS700016": "The application (client) ID was not found in this tenant: check it and the tenant.",
    "AADSTS7000218": "The app registration is not set to allow public client flows: turn that on under Authentication.",
}
_SECRETISH = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-.]+|[A-Za-z0-9+/_\-]{60,}")


def _clean(text: str, limit: int = 300) -> str:
    """Microsoft's words for a failure, cut to the sentence before its trace and correlation ids and with anything token-like removed."""
    text = re.split(r"\s*(?:Trace ID|Correlation ID|Timestamp):", text, maxsplit=1)[0]
    return _SECRETISH.sub("[removed]", " ".join(text.split()))[:limit]


def graph_detail(response: httpx.Response) -> str | None:
    """The error code and message in a Graph answer ("ErrorAccessDenied: Access is denied"), or None."""
    try:
        error = response.json().get("error")
    except ValueError:
        return None
    if isinstance(error, dict) and (error.get("code") or error.get("message")):
        return _clean(f"{error.get('code', '')}: {error.get('message', '')}".strip(": "))
    return None


class MailError(Exception):
    """A short code that is safe to store and show, whether trying again can help, whether the connection needs signing in again, and what Microsoft said (its error code and message)."""

    def __init__(self, code: str, *, retryable: bool = False, reconnect: bool = False, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.reconnect = reconnect
        self.detail = detail

    @property
    def message(self) -> str:
        return explain(self.code, self.detail)


def explain(code: str | None, detail: str | None) -> str:
    """The sentence for a failure: what it means, what Microsoft's error was, and what usually causes that one."""
    text = MESSAGES.get(code or "", "Microsoft refused the request.")
    if detail:
        hint = next((words for key, words in HINTS.items() if detail.startswith(key) or key in detail), None)
        text = f"{text} Microsoft said: {detail}" + (f" Usually: {hint}" if hint else "")
    return text


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def valid_tenant(value: str) -> str:
    value = value.strip()
    if not TENANT.fullmatch(value):
        raise ValueError("Enter the tenant ID (a GUID) or its domain name, like contoso.onmicrosoft.com")
    return value


def valid_client(value: str) -> str:
    value = value.strip()
    if not CLIENT.fullmatch(value):
        raise ValueError("Enter the application (client) ID from the app registration: a GUID")
    return value


def valid_address(value: str) -> str:
    value = value.strip()
    if not ADDRESS.fullmatch(value):
        raise ValueError(f"{value or 'An empty address'} is not an email address")
    return value


def _post_form(url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
    try:
        response = httpx.post(url, data=data, timeout=REQUEST_TIMEOUT, follow_redirects=False, trust_env=False)
    except (httpx.TransportError, OSError) as exc:
        raise MailError("unreachable", retryable=True) from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


def _token_error(body: dict[str, Any], status: int) -> MailError:
    code = str(body.get("error") or "")
    detail = _clean(str(body.get("error_description") or "")) or None  # "AADSTS65001: The user or administrator has not consented..."
    if code in {"invalid_grant", "interaction_required", "consent_required"}:
        return MailError("invalid_grant", reconnect=True, detail=detail)
    if code in MESSAGES:
        return MailError(code, reconnect=code in {"invalid_client", "unauthorized_client"}, detail=detail)
    if status >= 500 or status == 429:
        return MailError("throttled" if status == 429 else "unexpected", retryable=True, detail=detail)
    return MailError("invalid_request", detail=detail)


@dataclass(frozen=True)
class Connection:
    tenant_id: str
    client_id: str
    sender: str | None
    account: str | None
    state: str
    last_error: str | None
    last_detail: str | None
    connected_at: str | None
    pending: dict[str, Any] | None
    tokens: dict[str, Any] | None


def load(db: sqlite3.Connection) -> Connection | None:
    row = db.execute("SELECT * FROM email_connection WHERE singleton_id=1").fetchone()
    if row is None:
        return None
    return Connection(
        row["tenant_id"], row["client_id"], row["sender"], row["account"], row["state"], row["last_error"], row["last_detail"], row["connected_at"],
        json.loads(decrypt(row["pending_ciphertext"])) if row["pending_ciphertext"] else None,
        json.loads(decrypt(row["tokens_ciphertext"])) if row["tokens_ciphertext"] else None,
    )


def view(connection: Connection | None) -> dict[str, Any]:
    """What the settings page may see: never a token or the device code."""
    if connection is None:
        return {"state": "none"}
    result: dict[str, Any] = {"state": connection.state, "tenant_id": connection.tenant_id, "client_id": connection.client_id, "sender": connection.sender,
                              "account": connection.account, "connected_at": connection.connected_at}
    if connection.last_error:
        result["error"] = {"code": connection.last_error, "message": explain(connection.last_error, connection.last_detail), "detail": connection.last_detail,
                           "reconnect": connection.last_error in {"invalid_grant", "invalid_client", "unauthorized_client", "not_connected"}}
    if connection.state == "pending" and connection.pending:
        result["sign_in"] = {key: connection.pending[key] for key in ("user_code", "verification_uri", "expires_at", "interval")}
    return result


def _save(db: sqlite3.Connection, **values: Any) -> None:
    values["updated_at"] = _iso(_now())
    db.execute(f"UPDATE email_connection SET {','.join(f'{key}=?' for key in values)} WHERE singleton_id=1", tuple(values.values()))


def start(tenant_id: str, client_id: str, sender: str | None) -> dict[str, Any]:
    """Ask Microsoft for a device sign-in and keep it until it is finished or expires. Replaces any earlier connection."""
    tenant, client = valid_tenant(tenant_id), valid_client(client_id)
    sender = valid_address(sender) if sender else None
    status, body = _post_form(f"{LOGIN_URL}/{tenant}/oauth2/v2.0/devicecode", {"client_id": client, "scope": SCOPE})
    if status != 200 or not body.get("device_code") or not body.get("user_code"):
        raise _token_error(body, status)
    pending = {"device_code": body["device_code"], "user_code": str(body["user_code"]), "verification_uri": str(body.get("verification_uri") or "https://microsoft.com/devicelogin"),
               "expires_at": _iso(_now() + timedelta(seconds=int(body.get("expires_in", 900)))), "interval": max(int(body.get("interval", 5)), 1)}
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM email_connection")
        db.execute("INSERT INTO email_connection(singleton_id,tenant_id,client_id,sender,state,pending_ciphertext,updated_at) VALUES(1,?,?,?,'pending',?,?)",
                   (tenant, client, sender, encrypt(json.dumps(pending)), _iso(_now())))
        db.execute("COMMIT")
    return {key: pending[key] for key in ("user_code", "verification_uri", "expires_at", "interval")}


def _account_of(id_token: str | None) -> str | None:
    """The signed-in account's name from the sign-in token, for display only (nothing trusts it)."""
    try:
        payload = (id_token or "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        name = claims.get("preferred_username") or claims.get("email") or claims.get("upn")
        return str(name)[:254] if name else None
    except (IndexError, ValueError, TypeError):
        return None


def _tokens(body: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"access_token": body["access_token"], "refresh_token": body.get("refresh_token") or (previous or {}).get("refresh_token"),
            "expires_at": _iso(_now() + timedelta(seconds=int(body.get("expires_in", 3600))))}


def poll() -> dict[str, Any]:
    """One check whether the administrator has signed in yet. Returns the connection's view; call again after ``interval`` seconds while it is ``pending``."""
    with _lock, connect() as db:
        connection = load(db)
        if connection is None:
            return view(None)
        if connection.state != "pending" or not connection.pending:
            return view(connection)
        pending = connection.pending
        if datetime.fromisoformat(pending["expires_at"].replace("Z", "+00:00")) <= _now():
            _save(db, state="error", last_error="expired_token", pending_ciphertext=None)
            return view(load(db))
        status, body = _post_form(f"{LOGIN_URL}/{connection.tenant_id}/oauth2/v2.0/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": connection.client_id, "device_code": pending["device_code"]})
        if status == 200 and body.get("access_token") and body.get("refresh_token"):
            _save(db, state="connected", account=_account_of(body.get("id_token")), tokens_ciphertext=encrypt(json.dumps(_tokens(body))), pending_ciphertext=None,
                  last_error=None, connected_at=_iso(_now()))
            return view(load(db))
        error = str(body.get("error") or "")
        if error == "authorization_pending":
            return view(connection)
        if error == "slow_down":
            pending["interval"] = int(pending.get("interval", 5)) + 5
            _save(db, pending_ciphertext=encrypt(json.dumps(pending)))
            return view(load(db))
        failure = _token_error(body, status)
        if failure.retryable:
            return view(connection)  # Microsoft hiccuped: the sign-in is still open
        _save(db, state="error", last_error=failure.code, last_detail=failure.detail, pending_ciphertext=None)
        return view(load(db))


def disconnect() -> None:
    with connect() as db:
        db.execute("DELETE FROM email_connection")


def access_token(force: bool = False) -> tuple[str, Connection]:
    """A valid access token, refreshed when it is about to expire (``force`` refreshes now). A refresh Microsoft refuses marks the connection as needing sign-in."""
    with _lock, connect() as db:
        connection = load(db)
        if connection is None or connection.state == "pending" or not connection.tokens:
            raise MailError("not_connected", reconnect=True)
        tokens = connection.tokens
        expires = datetime.fromisoformat(tokens["expires_at"].replace("Z", "+00:00"))
        if not force and expires - REFRESH_MARGIN > _now():
            return tokens["access_token"], connection
        status, body = _post_form(f"{LOGIN_URL}/{connection.tenant_id}/oauth2/v2.0/token", {
            "grant_type": "refresh_token", "client_id": connection.client_id, "refresh_token": tokens["refresh_token"], "scope": SCOPE})
        if status == 200 and body.get("access_token"):
            fresh = _tokens(body, tokens)
            _save(db, state="connected", tokens_ciphertext=encrypt(json.dumps(fresh)), last_error=None)
            return fresh["access_token"], connection
        failure = _token_error(body, status)
        if failure.reconnect:
            _save(db, state="error", last_error=failure.code, last_detail=failure.detail)
        raise failure


def send(recipients: list[str], subject: str, text: str) -> None:
    """Send one plain-text message. Raises ``MailError``; ``retryable`` says whether trying again later can help."""
    if not recipients or len(recipients) > MAX_RECIPIENTS:
        raise MailError("rejected")
    message = {"message": {
        "subject": subject.replace("\r", " ").replace("\n", " ")[:MAX_SUBJECT],
        "body": {"contentType": "Text", "content": text[:MAX_BODY]},
        "toRecipients": [{"emailAddress": {"address": address}} for address in recipients],
    }, "saveToSentItems": False}
    for attempt in (0, 1):
        token, connection = access_token(force=bool(attempt))
        path = "/me/sendMail" if not connection.sender or connection.sender.lower() == (connection.account or "").lower() else f"/users/{quote(connection.sender, safe='@')}/sendMail"
        try:
            response = httpx.post(f"{GRAPH_URL}{path}", json=message, headers={"Authorization": f"Bearer {token}"}, timeout=REQUEST_TIMEOUT, follow_redirects=False, trust_env=False)
        except (httpx.TransportError, OSError) as exc:
            raise MailError("unreachable", retryable=True) from exc
        if response.status_code == 202:
            return
        detail = graph_detail(response)
        if response.status_code == 401 and not attempt:
            continue  # The token was refused (revoked or clock skew): one forced refresh, then try again
        if response.status_code == 401:
            raise MailError("invalid_grant", reconnect=True, detail=detail)
        if response.status_code == 403:
            raise MailError("forbidden", detail=detail)
        if response.status_code == 429 or response.status_code >= 500:
            raise MailError("throttled" if response.status_code == 429 else "unexpected", retryable=True, detail=detail)
        raise MailError("rejected", detail=detail or f"HTTP {response.status_code}")
    raise MailError("unexpected")  # pragma: no cover - the loop returns or raises


def note_failure(error: MailError) -> None:
    """Remember the last send failure where the settings page shows it (only a failure that needs a new sign-in changes the state)."""
    with connect() as db:
        if db.execute("SELECT 1 FROM email_connection WHERE singleton_id=1").fetchone() is not None:
            _save(db, last_error=error.code, last_detail=error.detail, **({"state": "error"} if error.reconnect else {}))


def note_success() -> None:
    with connect() as db:
        db.execute("UPDATE email_connection SET last_error=NULL,last_detail=NULL WHERE singleton_id=1 AND state='connected' AND last_error IS NOT NULL")


def compose(template: str | None, payload: dict[str, Any]) -> tuple[str, str]:
    """The subject and text of an alert email. The subject template may use {rule}, {camera}, {label}, {time}; the body always says the same things and links to the alarm."""
    from .alarms import label_text

    values = {"rule": payload.get("rule_name") or "Rule", "camera": payload.get("camera_name") or "a camera", "label": label_text(payload.get("label") or ""),
              "time": (payload.get("occurred_at") or "")[:19].replace("T", " ") + " UTC"}
    subject = template or "{rule}: {label} on {camera}"
    for key, value in values.items():
        subject = subject.replace("{" + key + "}", str(value))
    lines = [f"{values['rule']}", f"{values['label']} on {values['camera']}", f"At {values['time']}"]
    if payload.get("alarm_url"):
        lines += ["", f"Open the alarm (sign in required): {payload['alarm_url']}"]
    lines += ["", "No footage or camera addresses are included in this message."]
    return subject, "\n".join(lines)

