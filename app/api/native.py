"""The native mobile app (#140): device push registration, device sessions, and the administrator's FCM and APNs credentials."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app import device_sessions, native_push
from app.api.identity import start_session
from app.principal import Administrator, CurrentUser
from app.store import connect

router = APIRouter()


class DeviceRequest(BaseModel):
    platform: str = Field(pattern="^(android|ios)$")
    token: str = Field(min_length=20, max_length=4096)
    device_label: str = Field(default="", max_length=120)


class TokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=4096)


class DeviceSessionRequest(BaseModel):
    device_label: str = Field(default="", max_length=120)


class ResumeRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class PushSettingsRequest(BaseModel):
    """The credentials are write-only: send one to set it, an empty string to remove it, leave it out to keep what is saved."""
    fcm_service_account: str | None = Field(default=None, max_length=20000)
    apns_key: str | None = Field(default=None, max_length=5000)
    apns_key_id: str | None = Field(default=None, max_length=10)
    apns_team_id: str | None = Field(default=None, max_length=10)
    apns_bundle_id: str | None = Field(default=None, max_length=255)
    apns_production: bool | None = None


@router.post("/api/native/devices", status_code=status.HTTP_201_CREATED)
def register_device(payload: DeviceRequest, user: CurrentUser) -> dict[str, object]:
    try:
        return native_push.register(int(user["id"]), payload.platform, payload.token, payload.device_label.strip())
    except native_push.NativePushError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/api/native/devices/remove", status_code=status.HTTP_204_NO_CONTENT)
def remove_device(payload: TokenRequest, user: CurrentUser) -> Response:
    native_push.unregister(int(user["id"]), payload.token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/native/device-sessions", status_code=status.HTTP_201_CREATED)
def create_device_session(payload: DeviceSessionRequest, user: CurrentUser) -> dict[str, object]:
    """For a fully signed-in person: the secret the app keeps in the phone's secure storage. Shown once."""
    return device_sessions.create(int(user["id"]), payload.device_label.strip())


@router.post("/api/native/device-sessions/resume")
def resume_device_session(payload: ResumeRequest, response: Response, request: Request) -> dict[str, object]:
    """Exchange the phone's secret (after the person unlocks it) for a session and a replacement secret. Anything else is the same 401."""
    resumed = device_sessions.resume(payload.token)
    if resumed is None:
        raise HTTPException(status_code=401, detail="Sign in again")
    start_session(response, resumed["user_id"])
    return {"username": resumed["username"], "role": resumed["role"], "token": resumed["token"], "expires_at": resumed["expires_at"]}


@router.get("/api/native/device-sessions")
def list_device_sessions(user: CurrentUser) -> dict[str, object]:
    return {"sessions": device_sessions.listing(int(user["id"]))}


@router.delete("/api/native/device-sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_device_session(session_id: str, user: CurrentUser) -> Response:
    if not device_sessions.revoke(int(user["id"]), session_id):
        raise HTTPException(status_code=404, detail="Device session not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/native/push-settings")
def get_push_settings(_: Administrator) -> dict[str, object]:
    with connect() as db:
        return native_push.settings_view(db)


@router.put("/api/native/push-settings")
def put_push_settings(payload: PushSettingsRequest, user: Administrator) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            native_push.save_settings(db, payload.model_dump(exclude_none=True))
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                       (int(user["id"]), "native_push_updated", "native_push_settings", "1", json.dumps({"fcm": payload.fcm_service_account is not None, "apns": payload.apns_key is not None})))
        except native_push.NativePushError as exc:
            db.execute("ROLLBACK")
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        native_push.forget_tokens()
        return native_push.settings_view(db)
