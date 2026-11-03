"""Email alerts through Microsoft sign-in (#231). Administrators connect one Microsoft 365 mailbox with a device sign-in, send a test, and disconnect."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import ms_mail
from app.audit import record
from app.principal import Administrator
from app.store import connect

router = APIRouter()


class ConnectInput(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=253)
    client_id: str = Field(min_length=1, max_length=64)
    sender: str | None = Field(default=None, max_length=320)  # A shared mailbox to send as; left out, mail is sent as the signed-in account


class TestInput(BaseModel):
    to: str = Field(min_length=1, max_length=320)


def _state() -> dict[str, Any]:
    with connect() as db:
        return ms_mail.view(ms_mail.load(db))


@router.get("/api/admin/email")
def email_state(_: Administrator) -> dict[str, Any]:
    return {**_state(), "scope": "Mail.Send", "limits": {"per_rule_per_hour": 20, "per_hour": 100}}


@router.post("/api/admin/email/connect")
def connect_microsoft(payload: ConnectInput, user: Administrator) -> dict[str, Any]:
    """Start a device sign-in: the answer is the web address and the code to enter there. Then call ``/poll`` until it is no longer pending."""
    try:
        sign_in = ms_mail.start(payload.tenant_id, payload.client_id, payload.sender)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ms_mail.MailError as exc:
        raise HTTPException(status_code=502 if exc.retryable else 422, detail=exc.message) from None
    with connect() as db:
        record(db, int(user["id"]), "email_connect_started", "email", "microsoft", tenant=payload.tenant_id.strip(), sender=(payload.sender or "").strip() or None)
    return {"state": "pending", "sign_in": sign_in}


@router.post("/api/admin/email/poll")
def poll_sign_in(user: Administrator) -> dict[str, Any]:
    before = _state()["state"]
    result = ms_mail.poll()
    if before == "pending" and result["state"] != "pending":
        with connect() as db:
            record(db, int(user["id"]), "email_connected" if result["state"] == "connected" else "email_connect_failed", "email", "microsoft",
                   **({"account": result.get("account")} if result["state"] == "connected" else {"reason": (result.get("error") or {}).get("code")}))
    return result


@router.post("/api/admin/email/test")
def send_test(payload: TestInput, user: Administrator) -> dict[str, Any]:
    try:
        address = ms_mail.valid_address(payload.to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        ms_mail.send([address], "Test email from your NVR", "This is a test of email alerts from your NVR. If you can read it, alerts can reach you.")
    except ms_mail.MailError as exc:
        ms_mail.note_failure(exc)
        with connect() as db:
            record(db, int(user["id"]), "email_test_failed", "email", "microsoft", reason=exc.code)
        raise HTTPException(status_code=502 if exc.retryable else 409, detail=exc.message) from None
    ms_mail.note_success()
    with connect() as db:
        record(db, int(user["id"]), "email_test_sent", "email", "microsoft")
    return {"sent": True, **_state()}


@router.delete("/api/admin/email", status_code=204)
def disconnect(user: Administrator) -> None:
    ms_mail.disconnect()
    with connect() as db:
        record(db, int(user["id"]), "email_disconnected", "email", "microsoft")
