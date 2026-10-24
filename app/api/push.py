"""Push notifications: device subscriptions, each person's choices, and notification thumbnails."""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from app import push
from app.principal import CurrentUser
from app.store import connect

router = APIRouter()

_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=80, max_length=120)
    auth: str = Field(min_length=16, max_length=40)


class SubscriptionRequest(BaseModel):
    endpoint: str = Field(min_length=12, max_length=2048)
    keys: SubscriptionKeys
    device_label: str = Field(default="", max_length=120)


class UnsubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=12, max_length=2048)


class QuietHours(BaseModel):
    start: str
    end: str
    timezone: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def valid(self) -> "QuietHours":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        if not _TIME.match(self.start) or not _TIME.match(self.end) or self.start == self.end:
            raise ValueError("Quiet hours need different start and end times in HH:MM")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Choose a valid time zone for quiet hours") from exc
        return self


class PreferencesRequest(BaseModel):
    # None means every rule (or every camera group); a list limits notifications to those.
    rule_ids: list[str] | None = Field(default=None, max_length=500)
    group_ids: list[str] | None = Field(default=None, max_length=200)
    quiet_hours: QuietHours | None = None

    @field_validator("rule_ids", "group_ids")
    @classmethod
    def distinct(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else list(dict.fromkeys(value))


@router.get("/api/push/public-key")
def get_public_key(_: CurrentUser) -> dict[str, object]:
    return {"public_key": push.public_key()}


@router.get("/api/push/preferences")
def get_preferences(user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        rules = db.execute("SELECT id,name FROM rules WHERE enabled=1 ORDER BY name COLLATE NOCASE").fetchall()
        groups = db.execute("SELECT id,name FROM camera_groups ORDER BY name COLLATE NOCASE").fetchall()
        return {**push.preferences(db, int(user["id"])), "rule_options": [dict(row) for row in rules], "group_options": [dict(row) for row in groups]}


@router.put("/api/push/preferences")
def put_preferences(payload: PreferencesRequest, user: CurrentUser) -> dict[str, object]:
    quiet = payload.quiet_hours.model_dump() if payload.quiet_hours else None
    saved = push.save_preferences(int(user["id"]), payload.rule_ids, payload.group_ids, quiet)
    return saved


@router.post("/api/push/subscriptions", status_code=status.HTTP_201_CREATED)
def post_subscription(payload: SubscriptionRequest, user: CurrentUser) -> dict[str, object]:
    try:
        return push.subscribe(int(user["id"]), payload.endpoint, payload.keys.p256dh, payload.keys.auth, payload.device_label.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/push/subscriptions/remove", status_code=status.HTTP_204_NO_CONTENT)
def remove_subscription(payload: UnsubscribeRequest, user: CurrentUser) -> Response:
    push.unsubscribe(int(user["id"]), payload.endpoint)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/push/thumbnails/{token}", include_in_schema=False)
def get_thumbnail(token: str) -> Response:
    """A notification's thumbnail: public, because the device fetches it without the session, but
    only through a short-lived link that names one alarm."""
    try:
        alarm_id = push.verify_thumbnail_token(token)
    except LookupError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    from app.retention import acquire_playback, release_playback
    from app.storage import recording_file_path

    with connect() as db:
        alarm = db.execute("SELECT camera_id,occurred_at FROM alarms WHERE id=?", (alarm_id,)).fetchone()
        segment = db.execute(
            "SELECT relative_path,starts_at FROM recording_segments WHERE camera_id=? AND starts_at<=? AND ends_at>? AND unavailable_at IS NULL ORDER BY starts_at DESC LIMIT 1",
            (alarm["camera_id"], alarm["occurred_at"], alarm["occurred_at"])).fetchone() if alarm else None
    if segment is None or not shutil.which("ffmpeg"):
        raise HTTPException(status_code=404, detail="No recorded footage covers this alarm yet")
    try:
        path = recording_file_path(segment["relative_path"])
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Recorded footage is unavailable") from exc
    offset = max(0.0, (datetime.fromisoformat(alarm["occurred_at"]) - datetime.fromisoformat(segment["starts_at"])).total_seconds())
    acquire_playback(path)
    try:
        result = subprocess.run(
            [shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{offset:.3f}", "-i", str(path), "-frames:v", "1",
             "-vf", "scale=640:-2:force_original_aspect_ratio=decrease", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=15, check=False)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=503, detail="The thumbnail could not be extracted in time") from exc
    finally:
        release_playback(path)
    if result.returncode != 0 or not result.stdout.startswith(b"\xff\xd8"):
        raise HTTPException(status_code=404, detail="The thumbnail could not be extracted")
    return Response(result.stdout, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff"})
