"""How the NVR is being reached, and what is wrong with that (#132). For administrators, in Settings."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app import mfa, network
from app.principal import Administrator, secure_cookie
from app.store import connect

router = APIRouter()


def warnings(info: dict[str, Any], policy: str, remote_mfa: bool, role: str) -> list[dict[str, str]]:
    """What is unsafe or likely wrong about the way this request arrived, worst first."""
    found: list[dict[str, str]] = []

    def add(severity: str, code: str, message: str) -> None:
        found.append({"severity": severity, "code": code, "message": message})

    exposed = info["remote"] or bool(info["trusted_proxies"]) or bool(info["public_url"])
    if info["remote"] and not info["https"]:
        add("bad", "plain_http_remote", f"This page was opened over plain HTTP from {info['client_address']}, outside the local network: passwords and video can be read on the way. "
                                        "Put the NVR behind a tunnel or proxy that serves HTTPS, and set NVR_PUBLIC_URL to its https address.")
    if (info["public_url"] or "").lower().startswith("http://") and not info["remote"]:
        add("warn", "public_url_plain_http", "NVR_PUBLIC_URL is a plain http:// address: anyone using it from outside the local network sends passwords unprotected.")
    if exposed and not (remote_mfa or policy == "all" or (policy == "administrators" and role == "administrator")):
        add("warn", "no_second_factor", "Remote sessions are not required to use a second factor: a password alone opens the NVR from outside the local network. "
                                        "Turn on the second factor for remote sessions below, or require one for everybody in Users & access.")
    forwarded = any(info["forwarded"].values())
    if forwarded and not info["peer_is_trusted_proxy"] and not info["legacy_forwarded_trust"]:
        add("warn", "forwarded_headers_ignored", f"Forwarded headers arrived from {info['peer_address']}, which is not a trusted proxy, so they were ignored and everybody behind it looks like one address "
                                                 "(sign-in limits are shared, and HTTPS cannot be seen). If it is your reverse proxy or tunnel, add its address to NVR_TRUSTED_PROXIES.")
    if info["legacy_forwarded_trust"] and not info["trusted_proxies"]:
        add("warn", "legacy_forwarded_trust", "NVR_TRUST_FORWARDED_FOR believes the forwarded address of whoever sends it, so anybody who can reach the NVR directly can pick their own address. "
                                              "List your proxy in NVR_TRUSTED_PROXIES instead.")
    if info["invalid_trusted_proxies"]:
        add("warn", "invalid_trusted_proxies", "These NVR_TRUSTED_PROXIES entries are not addresses or networks and were ignored: " + ", ".join(info["invalid_trusted_proxies"]) + ".")
    if info["https"] and not info["cookie_secure"]:
        add("warn", "cookie_not_secure", "Sessions are used over HTTPS but the session cookie is not marked Secure. Set NVR_COOKIE_SECURE=true (or NVR_PUBLIC_URL to the https address).")
    return sorted(found, key=lambda item: item["severity"] != "bad")


@router.get("/api/admin/remote-access")
def remote_access(request: Request, user: Administrator) -> dict[str, Any]:
    """How this request reached the NVR, what the NVR trusts about it, and the warnings that follow."""
    with connect() as db:
        policy, remote_mfa = mfa.policy(db), mfa.remote_required(db)
    info = {**network.describe(request), "cookie_secure": secure_cookie()}
    return {**info, "mfa_policy": policy, "remote_mfa": remote_mfa, "warnings": warnings(info, policy, remote_mfa, user["role"])}
