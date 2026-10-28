"""Push to the native app's devices (#140): Firebase Cloud Messaging for Android, the Apple Push Notification service for iOS.

A device registers its token under the person who signed in; from then on it is an ordinary push subscription (``kind`` ``fcm`` or ``apns``) with
that person's rule, group and quiet-hour choices, the access check at send time, and the shared queue with its retries (see ``push``). The
administrator supplies the FCM service account and the APNs key once; both are stored encrypted and never returned.

Neither service takes the web-push encryption: a message is a small JSON document sent over TLS with a short-lived token the NVR signs itself
(an RS256 JWT exchanged for an OAuth token for FCM, an ES256 JWT for APNs). The title, body and a link to the alarm are all a notification carries; the
picture is fetched by the app through the signed thumbnail link, as for web push.
"""
from __future__ import annotations

import base64
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from .credentials import decrypt, encrypt
from .store import connect

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
APNS_URLS = {True: "https://api.push.apple.com", False: "https://api.sandbox.push.apple.com"}
TTL_SECONDS = 3600
PLATFORMS = {"android": "fcm", "ios": "apns"}
_FCM_TOKEN = re.compile(r"^[A-Za-z0-9:_.\-]{20,4096}$")
_APNS_TOKEN = re.compile(r"^[0-9A-Fa-f]{64,200}$")
_APPLE_ID = re.compile(r"^[A-Z0-9]{10}$")
_PROJECT = re.compile(r"^[a-z][a-z0-9\-]{4,29}$")
_BUNDLE = re.compile(r"^[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")


class NativePushError(ValueError):
    """A device or the credentials cannot be saved as given."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# --- devices ----------------------------------------------------------------------------------------------------------------------------------

def register(user_id: int, platform: str, token: str, device_label: str) -> dict[str, Any]:
    """Subscribe a phone's push token for a person. A token that changes hands (another person signs in on the phone) moves to its new owner."""
    kind = PLATFORMS.get(platform)
    token = token.strip()
    if kind is None:
        raise NativePushError("The platform is android or ios")
    if not (_FCM_TOKEN if kind == "fcm" else _APNS_TOKEN).match(token):
        raise NativePushError("The device token is not one the push service issues")
    endpoint = f"{kind}:{token.lower() if kind == 'apns' else token}"
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            existing = db.execute("SELECT id FROM push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
            subscription_id = existing["id"] if existing else str(uuid.uuid4())
            db.execute("INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh,auth,device_label,kind) VALUES(?,?,?,'','',?,?) "
                       "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,device_label=excluded.device_label",
                       (subscription_id, user_id, endpoint, device_label, kind))
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                       (user_id, "push_subscribed", "push_subscription", subscription_id, json.dumps({"device": device_label, "platform": platform})))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {"id": subscription_id, "platform": platform, "device_label": device_label}


def unregister(user_id: int, token: str) -> bool:
    token = token.strip()
    with connect() as db:
        return db.execute("DELETE FROM push_subscriptions WHERE user_id=? AND kind IN ('fcm','apns') AND endpoint IN (?,?,?)",
                          (user_id, f"fcm:{token}", f"apns:{token}", f"apns:{token.lower()}")).rowcount > 0


# --- the administrator's credentials -----------------------------------------------------------------------------------------------------------

def settings_view(db: sqlite3.Connection) -> dict[str, Any]:
    """What the administrator sees: whether each service is set up and the identifiers that are not secret, never a key."""
    row = db.execute("SELECT * FROM native_push_settings WHERE singleton_id=1").fetchone()
    return {"fcm_configured": row["fcm_credentials_ciphertext"] is not None, "fcm_project_id": row["fcm_project_id"],
            "apns_configured": row["apns_key_ciphertext"] is not None, "apns_key_id": row["apns_key_id"], "apns_team_id": row["apns_team_id"],
            "apns_bundle_id": row["apns_bundle_id"], "apns_production": bool(row["apns_production"]), "updated_at": row["updated_at"]}


def save_settings(db: sqlite3.Connection, fields: dict[str, Any]) -> None:
    """Change the credentials inside the caller's transaction; a key left out is kept as saved, and sending an empty one removes it."""
    row = db.execute("SELECT * FROM native_push_settings WHERE singleton_id=1").fetchone()
    project, fcm_blob = row["fcm_project_id"], row["fcm_credentials_ciphertext"]
    if "fcm_service_account" in fields:
        text = (fields["fcm_service_account"] or "").strip()
        if not text:
            project, fcm_blob = None, None
        else:
            account = _service_account(text)
            project, fcm_blob = account["project_id"], encrypt(text)
    apns_blob, key_id, team, bundle = row["apns_key_ciphertext"], row["apns_key_id"], row["apns_team_id"], row["apns_bundle_id"]
    if "apns_key" in fields:
        pem = (fields["apns_key"] or "").strip()
        if not pem:
            apns_blob = None
        else:
            try:
                key = serialization.load_pem_private_key(pem.encode(), password=None)
            except (ValueError, TypeError):
                raise NativePushError("The APNs key is not a PEM private key (the .p8 file)") from None
            if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
                raise NativePushError("The APNs key must be an elliptic-curve P-256 key (the .p8 file Apple issues)")
            apns_blob = encrypt(pem)
    key_id = fields.get("apns_key_id", key_id)
    team = fields.get("apns_team_id", team)
    bundle = fields.get("apns_bundle_id", bundle)
    if key_id and not _APPLE_ID.match(key_id):
        raise NativePushError("The APNs key id is 10 letters and digits")
    if team and not _APPLE_ID.match(team):
        raise NativePushError("The Apple team id is 10 letters and digits")
    if bundle and not _BUNDLE.match(bundle):
        raise NativePushError("The bundle id looks like com.example.nvr")
    if apns_blob is not None and not (key_id and team and bundle):
        raise NativePushError("APNs needs the key id, the team id and the bundle id with the key")
    db.execute("UPDATE native_push_settings SET fcm_project_id=?,fcm_credentials_ciphertext=?,apns_key_ciphertext=?,apns_key_id=?,apns_team_id=?,apns_bundle_id=?,apns_production=?,updated_at=? WHERE singleton_id=1",
               (project, fcm_blob, apns_blob, key_id or None, team or None, bundle or None, int(fields.get("apns_production", row["apns_production"])), _now().isoformat()))


def _service_account(text: str) -> dict[str, Any]:
    try:
        account = json.loads(text)
        key = serialization.load_pem_private_key(account["private_key"].encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or not account["client_email"] or not account["project_id"]:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise NativePushError("Paste the whole service-account JSON file from Firebase (Project settings, Service accounts)") from None
    if not _PROJECT.match(str(account["project_id"])):
        raise NativePushError("The Firebase project id in the file is not one Firebase issues")
    return account


# --- signing ----------------------------------------------------------------------------------------------------------------------------------

def _rs256(claims: dict[str, Any], pem: str) -> str:
    signing_input = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + "." + _b64(json.dumps(claims).encode())
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    return signing_input + "." + _b64(key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()))


def _es256(claims: dict[str, Any], pem: str, key_id: str) -> str:
    signing_input = _b64(json.dumps({"alg": "ES256", "kid": key_id}).encode()) + "." + _b64(json.dumps(claims).encode())
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    return signing_input + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


_tokens: dict[str, tuple[str, float]] = {}


def _cached(name: str, make) -> str:
    token, expires = _tokens.get(name, ("", 0.0))
    if token and expires > time.time():
        return token
    token, lifetime = make()
    _tokens[name] = (token, time.time() + lifetime)
    return token


def forget_tokens() -> None:
    _tokens.clear()


# --- sending ----------------------------------------------------------------------------------------------------------------------------------

def _credentials() -> sqlite3.Row:
    with connect() as db:
        return db.execute("SELECT * FROM native_push_settings WHERE singleton_id=1").fetchone()


def send(client: httpx.Client, kind: str, token: str, payload: dict[str, Any]) -> tuple[int | None, str | None]:
    """Send one notification. Returns what ``push`` expects: a status for the outcome (410 when the device is gone, so it is forgotten),
    or ``None`` with an error to retry. A rejected credential never forgets the device."""
    return (_send_fcm if kind == "fcm" else _send_apns)(client, token, payload)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _send_fcm(client: httpx.Client, token: str, payload: dict[str, Any]) -> tuple[int | None, str | None]:
    settings = _credentials()
    if settings["fcm_credentials_ciphertext"] is None:
        return None, "invalid: Firebase Cloud Messaging is not set up"
    account = json.loads(decrypt(settings["fcm_credentials_ciphertext"]))

    def access_token() -> tuple[str, float]:
        now = int(time.time())
        assertion = _rs256({"iss": account["client_email"], "scope": FCM_SCOPE, "aud": FCM_TOKEN_URL, "iat": now, "exp": now + 3000}, account["private_key"])
        response = client.post(FCM_TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        answer = _json(response)
        if not response.is_success or "access_token" not in answer:
            raise PermissionError("Firebase rejected the service account")
        return answer["access_token"], max(60.0, float(answer.get("expires_in", 3600)) - 120)

    try:
        bearer = _cached("fcm", access_token)
    except PermissionError as exc:
        return None, str(exc)
    message = {"message": {"token": token, "notification": {"title": payload["title"], "body": payload["body"]},
                           "data": {key: str(payload[key]) for key in ("alarm_id", "url", "thumbnail_url", "occurred_at") if payload.get(key)},
                           "android": {"priority": "HIGH", "ttl": f"{TTL_SECONDS}s", "notification": {"channel_id": "alarms", "tag": payload.get("alarm_id", "alarm")}}}}
    response = client.post(FCM_SEND_URL.format(project=settings["fcm_project_id"]), json=message, headers={"Authorization": f"Bearer {bearer}"})
    if response.is_success:
        return response.status_code, None
    if response.status_code in (401, 403):
        _tokens.pop("fcm", None)
        return None, "Firebase rejected the credentials"
    # Only Firebase saying the token is unknown forgets the phone; a wrong project (also a 404) must not wipe every registration.
    error = _json(response).get("error", {})
    codes = {detail.get("errorCode") for detail in error.get("details", []) if isinstance(detail, dict)} if isinstance(error, dict) else set()
    message = str(error.get("message", "")).lower() if isinstance(error, dict) else ""
    if "UNREGISTERED" in codes or (response.status_code == 400 and "registration token" in message):
        return 410, "The device is no longer registered"
    if response.status_code in (401, 403, 404):  # Never a status that makes the queue forget the phone
        return None, f"HTTP {response.status_code}" + (" (check the Firebase project)" if response.status_code == 404 else "")
    return response.status_code, f"HTTP {response.status_code}"


def _send_apns(client: httpx.Client, token: str, payload: dict[str, Any]) -> tuple[int | None, str | None]:
    settings = _credentials()
    if settings["apns_key_ciphertext"] is None:
        return None, "invalid: Apple push is not set up"
    pem = decrypt(settings["apns_key_ciphertext"])
    bearer = _cached("apns", lambda: (_es256({"iss": settings["apns_team_id"], "iat": int(time.time())}, pem, settings["apns_key_id"]), 50 * 60))
    body = {"aps": {"alert": {"title": payload["title"], "body": payload["body"]}, "sound": "default", "mutable-content": 1, "thread-id": payload.get("alarm_id", "alarm")},
            **{key: payload[key] for key in ("alarm_id", "url", "thumbnail_url", "occurred_at") if payload.get(key)}}
    response = client.post(f"{APNS_URLS[bool(settings['apns_production'])]}/3/device/{token}", json=body, headers={
        "authorization": f"bearer {bearer}", "apns-topic": settings["apns_bundle_id"], "apns-push-type": "alert", "apns-priority": "10",
        "apns-expiration": str(int(time.time()) + TTL_SECONDS)})
    if response.is_success:
        return response.status_code, None
    reason = str(_json(response).get("reason", ""))
    # Only Apple saying the token is no longer registered forgets the phone: a bad token or topic is as likely the wrong environment or bundle id, which is the NVR's to fix.
    if response.status_code == 410 or reason == "Unregistered":
        return 410, "The device is no longer registered"
    if response.status_code in (401, 403):
        _tokens.pop("apns", None)
        return None, f"Apple rejected the credentials ({reason or response.status_code})"
    return response.status_code, f"HTTP {response.status_code} {reason}".strip()
