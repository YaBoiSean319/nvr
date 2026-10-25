"""Access grants: which cameras an operator may see, and until when. Administrators manage them."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from app.audit import record
from app.principal import Administrator
from app.store import connect

router = APIRouter()

MAX_GRANTS_PER_SUBJECT = 50


class GrantInput(BaseModel):
    scope_type: Literal["all", "group"]
    group_id: str | None = Field(default=None, max_length=64)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def future_and_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("The expiry must include a time zone")
        if value <= datetime.now(timezone.utc):
            raise ValueError("The expiry must be in the future")
        return value


class SubjectGrants(BaseModel):
    subject_type: Literal["user", "role"]
    subject: str = Field(min_length=1, max_length=200)
    grants: list[GrantInput] = Field(max_length=MAX_GRANTS_PER_SUBJECT)


def _view(db: sqlite3.Connection, row: sqlite3.Row, now: str) -> dict[str, Any]:
    names = {"user": None, "group": None}
    if row["subject_type"] == "user":
        user = db.execute("SELECT username FROM users WHERE id=?", (int(row["subject"]),)).fetchone()
        names["user"] = user["username"] if user else None
    if row["group_id"]:
        group = db.execute("SELECT name FROM camera_groups WHERE id=?", (row["group_id"],)).fetchone()
        names["group"] = group["name"] if group else None
    return {"id": row["id"], "subject_type": row["subject_type"], "subject": row["subject"], "subject_name": names["user"] or row["subject"],
            "scope_type": row["scope_type"], "group_id": row["group_id"], "group_name": names["group"], "expires_at": row["expires_at"],
            "default": bool(row["default_grant"]),
            "expired": bool(row["expires_at"] and row["expires_at"] <= now), "created_at": row["created_at"]}


def _snapshot(db: sqlite3.Connection, subject_type: str, subject: str) -> list[dict[str, Any]]:
    return [{"scope_type": row["scope_type"], "group_id": row["group_id"], "expires_at": row["expires_at"]}
            for row in db.execute("SELECT * FROM access_grants WHERE subject_type=? AND subject=? ORDER BY id", (subject_type, subject))]


@router.get("/api/admin/access-grants")
def list_grants(_: Administrator) -> dict[str, object]:
    """Every grant, the operators and Entra roles they can be given to, and the camera groups they can name."""
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        grants = [_view(db, row, now) for row in db.execute("SELECT * FROM access_grants ORDER BY subject_type, id")]
        operators = [{"id": row["id"], "username": row["username"], "local": row["provider_issuer"] is None}
                     for row in db.execute("SELECT id,username,provider_issuer FROM users WHERE role='operator' ORDER BY username COLLATE NOCASE")]
        roles = {role for row in db.execute("SELECT provider_roles_json FROM users") for role in json.loads(row[0] or "[]") if isinstance(role, str)}
        roles |= {row["subject"] for row in db.execute("SELECT subject FROM access_grants WHERE subject_type='role'")}
        groups = [{"id": row["id"], "name": row["name"]} for row in db.execute("SELECT id,name FROM camera_groups ORDER BY name COLLATE NOCASE")]
    return {"grants": grants, "operators": operators, "roles": sorted(roles), "groups": groups}


@router.put("/api/admin/access-grants")
def replace_grants(payload: SubjectGrants, admin: Administrator) -> dict[str, object]:
    """Replace everything one operator (or Entra role) is granted, in one audited step. No grants means no cameras."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if payload.subject_type == "user":
                try:
                    user = db.execute("SELECT id,username,role FROM users WHERE id=?", (int(payload.subject),)).fetchone()
                except ValueError:
                    user = None
                if user is None:
                    raise HTTPException(status_code=404, detail="User not found")
                if user["role"] == "administrator":
                    raise HTTPException(status_code=422, detail="Administrators already have access to every camera")
                label = user["username"]
            else:
                label = payload.subject.strip()
                if not label:
                    raise HTTPException(status_code=422, detail="Name the Entra role")
            seen = set()
            for grant in payload.grants:
                if grant.scope_type == "group":
                    if not grant.group_id or db.execute("SELECT 1 FROM camera_groups WHERE id=?", (grant.group_id,)).fetchone() is None:
                        raise HTTPException(status_code=422, detail="Choose an existing camera group")
                elif grant.group_id:
                    raise HTTPException(status_code=422, detail="A grant for every camera names no group")
                key = (grant.scope_type, grant.group_id)
                if key in seen:
                    raise HTTPException(status_code=422, detail="Each group can be granted only once")
                seen.add(key)
            subject = str(user["id"]) if payload.subject_type == "user" else label
            before = _snapshot(db, payload.subject_type, subject)
            db.execute("DELETE FROM access_grants WHERE subject_type=? AND subject=?", (payload.subject_type, subject))
            for grant in payload.grants:
                db.execute("INSERT INTO access_grants(subject_type,subject,scope_type,group_id,expires_at,created_by) VALUES(?,?,?,?,?,?)",
                           (payload.subject_type, subject, grant.scope_type, grant.group_id,
                            grant.expires_at.astimezone(timezone.utc).isoformat() if grant.expires_at else None, admin.id))
            after = _snapshot(db, payload.subject_type, subject)
            record(db, admin.id, "access_grants_changed", payload.subject_type, subject, subject_name=label, before=before, after=after)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {"subject_type": payload.subject_type, "subject": subject, "grants": after}


@router.delete("/api/admin/access-grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_grant(grant_id: int, admin: Administrator) -> Response:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM access_grants WHERE id=?", (grant_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Grant not found")
            db.execute("DELETE FROM access_grants WHERE id=?", (grant_id,))
            record(db, admin.id, "access_grant_deleted", row["subject_type"], row["subject"], scope_type=row["scope_type"], group_id=row["group_id"], expires_at=row["expires_at"])
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
