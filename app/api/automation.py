"""Administrator management of scoped API tokens for automation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.api_tokens import SCOPES, create_token, list_tokens, revoke_token
from app.principal import Administrator, Principal, Scope

router = APIRouter()


class APITokenCreateRequest(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    scopes: list[Scope] = Field(min_length=1, max_length=len(SCOPES))
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def expiry_in_future(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Expiry must include a time zone")
        if value <= datetime.now(timezone.utc):
            raise ValueError("Expiry must be in the future")
        return value


def _session_only(user: Principal) -> None:
    if user.token_id is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API tokens cannot manage API tokens")


@router.get("/api/admin/api-tokens")
def get_api_tokens(user: Administrator) -> dict[str, object]:
    _session_only(user)
    return {"tokens": list_tokens(), "scopes": list(SCOPES)}


@router.post("/api/admin/api-tokens", status_code=status.HTTP_201_CREATED)
def post_api_token(payload: APITokenCreateRequest, user: Administrator) -> dict[str, object]:
    _session_only(user)
    token, secret = create_token(payload.name, list(payload.scopes), payload.expires_at, int(user["id"]))
    # The secret is only ever returned here.
    return {"token": token, "secret": secret}


@router.delete("/api/admin/api-tokens/{token_id}")
def delete_api_token(token_id: str, user: Administrator) -> dict[str, object]:
    _session_only(user)
    token = revoke_token(token_id, int(user["id"]))
    if token is None:
        raise HTTPException(status_code=404, detail="API token not found")
    return {"token": token}
