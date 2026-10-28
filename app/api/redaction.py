"""Redaction reviews: find the faces and plates in a range, adjust what is blurred, confirm, then export with it (#130)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, model_validator

from app import redaction, viewing_audit
from app.exports import MAX_EXPORT_CAMERAS
from app.principal import CurrentUser, requires_scope

router = APIRouter()


def _review_for(review_id: str, user):
    """A review of cameras this person may all see and made by them (or any, for an administrator); any other is indistinguishable from none."""
    row = redaction.get_review(review_id, int(user["id"]), user["role"] == "administrator")
    if row is None or (not user.access.unrestricted and not all(user.access.can_see(camera_id) for camera_id in json.loads(row["camera_ids_json"]))):
        raise HTTPException(status_code=404, detail="Redaction review not found")
    return row


class ReviewRequest(BaseModel):
    camera_ids: list[str] = Field(min_length=1, max_length=MAX_EXPORT_CAMERAS)
    start: datetime
    end: datetime
    kinds: list[Literal["faces", "plates"]] = Field(default_factory=lambda: ["faces", "plates"], min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_interval(self) -> "ReviewRequest":
        if len(set(self.camera_ids)) != len(self.camera_ids):
            raise ValueError("Choose each camera only once")
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError("The range must include timezone-aware start and end times")
        return self


class Keyframe(BaseModel):
    t: float = Field(ge=0)
    x: float
    y: float
    width: float
    height: float


class ManualRegion(BaseModel):
    camera_id: str = Field(max_length=64)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    keyframes: list[Keyframe] = Field(default_factory=list, max_length=redaction.MAX_KEYFRAMES)
    follow_track_id: str | None = Field(default=None, max_length=64)
    scale: float = 1.0


class ReviewEdit(BaseModel):
    keep: list[str] | None = Field(default=None, max_length=redaction.MAX_TRACKS_PER_CAMERA * MAX_EXPORT_CAMERAS)
    manual: list[ManualRegion] | None = Field(default=None, max_length=redaction.MAX_MANUAL)


@router.get("/api/redaction/models")
@requires_scope("export")
def redaction_models(user: CurrentUser) -> dict[str, Any]:
    """Which kinds of blur the installed models can find; empty when none is installed."""
    return {"kinds": redaction.available_kinds()}


@router.post("/api/redaction/reviews", status_code=status.HTTP_202_ACCEPTED)
@requires_scope("export")
def create_review(payload: ReviewRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    # A camera this person may not see goes down exactly the path of one that does not exist.
    swapped = [camera_id if user.access.can_see(camera_id) else f"hidden-{uuid.uuid4()}" for camera_id in payload.camera_ids]
    try:
        review_id = redaction.create_review(int(user["id"]), swapped, payload.start.astimezone(timezone.utc), payload.end.astimezone(timezone.utc), list(payload.kinds))
    except LookupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return redaction.view(redaction.get_review(review_id, int(user["id"])))


@router.get("/api/redaction/reviews/{review_id}")
@requires_scope("export")
def get_review(review_id: str, user: CurrentUser) -> dict[str, Any]:
    return redaction.view(_review_for(review_id, user))


@router.patch("/api/redaction/reviews/{review_id}")
@requires_scope("export")
def edit_review(review_id: str, payload: ReviewEdit, user: CurrentUser) -> dict[str, Any]:
    row = _review_for(review_id, user)
    try:
        return redaction.update_review(row, int(user["id"]), payload.keep, None if payload.manual is None else [region.model_dump() for region in payload.manual])
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (redaction.RedactionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/redaction/reviews/{review_id}/confirm")
@requires_scope("export")
def confirm_review(review_id: str, user: CurrentUser) -> dict[str, Any]:
    row = _review_for(review_id, user)
    try:
        return redaction.confirm_review(row, int(user["id"]))
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/redaction/reviews/{review_id}/frame")
@requires_scope("export")
def review_frame(review_id: str, user: CurrentUser, camera_id: str = Query(max_length=64), t: float = Query(ge=0, le=86400)) -> Response:
    """A picture from the footage with the planned blurs outlined, for the reviewer. Looking at footage is audited like any other view."""
    row = _review_for(review_id, user)
    if camera_id not in json.loads(row["camera_ids_json"]):
        raise HTTPException(status_code=404, detail="Redaction review not found")
    if row["state"] not in {"ready", "confirmed", "used"}:
        raise HTTPException(status_code=409, detail="The review is not ready yet")
    viewing_audit.safely(viewing_audit.record_events, int(user["id"]), [camera_id], "export", review_id, timedelta(minutes=10))
    try:
        image = redaction.preview_frame(review_id, row, camera_id, t)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
