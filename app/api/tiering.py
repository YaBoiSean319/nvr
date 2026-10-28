"""Storage tiering (#144): cold tiers (a local path or an S3-compatible bucket) and the policy that moves a storage volume's older footage to one. Administrators manage them."""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator, model_validator

from app import tiering
from app.audit import record
from app.principal import Administrator
from app.recording import recordings_path
from app.s3 import S3Error
from app.storage import configured_volumes
from app.store import connect
from app.webhooks import validate_target_url

router = APIRouter()

BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
PREFIX = re.compile(r"^[A-Za-z0-9._/-]{0,128}$")
MAX_TIERS = 20


def check_endpoint(url: str, allow_private: bool) -> str:
    try:
        return validate_target_url(url, allow_private).rstrip("/")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc).replace("Webhook", "Object store").replace("webhook", "object store")) from None


class TierInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["local", "s3"]
    local_path: str | None = Field(default=None, max_length=1024)
    s3_endpoint: str | None = Field(default=None, max_length=2048)
    s3_bucket: str | None = Field(default=None, max_length=63)
    s3_region: str = Field(default="us-east-1", min_length=1, max_length=64)
    s3_prefix: str = Field(default="", max_length=128)
    access_key: str | None = Field(default=None, max_length=256)  # Write-only: left out, the saved ones are kept
    secret_key: str | None = Field(default=None, max_length=256)
    allow_private: bool = False

    @field_validator("name")
    @classmethod
    def trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a name for the tier")
        return value

    @field_validator("s3_prefix")
    @classmethod
    def safe_prefix(cls, value: str) -> str:
        value = value.strip().strip("/")
        if not PREFIX.fullmatch(value) or ".." in value.split("/"):
            raise ValueError("The prefix may hold letters, digits and . _ - /")
        return value

    @model_validator(mode="after")
    def complete(self) -> "TierInput":
        if self.kind == "local" and not self.local_path:
            raise ValueError("Enter the folder the cold tier is")
        if self.kind == "s3":
            if not self.s3_endpoint or not self.s3_bucket:
                raise ValueError("Enter the object store's address and the bucket")
            if not BUCKET.fullmatch(self.s3_bucket):
                raise ValueError("The bucket name is not valid (3 to 63 lower-case letters, digits, dots and hyphens)")
            if bool(self.access_key) != bool(self.secret_key):
                raise ValueError("Enter both the access key and the secret key")
        return self


class PolicyInput(BaseModel):
    tier_id: str = Field(min_length=1, max_length=64)
    after_days: int = Field(ge=1, le=3650)
    rate_kbps: int = Field(default=tiering.DEFAULT_RATE_KBPS, ge=64, le=10_000_000)
    enabled: bool = True


def _tier_view(row, counts: dict[str, tuple[int, int]]) -> dict[str, Any]:
    segments, size = counts.get(row["id"], (0, 0))
    return {"id": row["id"], "name": row["name"], "kind": row["kind"], "local_path": row["local_path"], "s3_endpoint": row["s3_endpoint"], "s3_bucket": row["s3_bucket"],
            "s3_region": row["s3_region"], "s3_prefix": row["s3_prefix"], "has_credentials": row["credentials_ciphertext"] is not None, "allow_private": bool(row["allow_private"]),
            "last_checked_at": row["last_checked_at"], "last_error": row["last_error"], "failures": row["failures"], "segments": segments, "bytes": size, "created_at": row["created_at"]}


def _view(db) -> dict[str, Any]:
    counts = {row[0]: (row[1], row[2] or 0) for row in db.execute("SELECT tier_id,COUNT(*),SUM(size_bytes) FROM recording_segments WHERE tier_id IS NOT NULL GROUP BY tier_id")}
    pending = db.execute("SELECT COUNT(*) FROM tier_moves").fetchone()[0]
    cache = tiering.cache_dir()
    cached = sum(path.stat().st_size for path in cache.glob("*.mp4")) if cache.is_dir() else 0
    return {"tiers": [_tier_view(row, counts) for row in db.execute("SELECT * FROM cold_tiers ORDER BY name COLLATE NOCASE,id")],
            "policies": [dict(row) | {"enabled": bool(row["enabled"])} for row in db.execute("SELECT * FROM tier_policies ORDER BY volume_id")],
            "volumes": sorted(configured_volumes()), "moves_in_progress": pending, "cache": {"bytes": cached, "limit_bytes": tiering.cache_limit()}}


@router.get("/api/admin/tiering")
def get_tiering(_: Administrator) -> dict[str, Any]:
    with connect() as db:
        return _view(db)


def _local_path(text: str) -> str:
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise HTTPException(status_code=422, detail="The cold tier's folder must be an absolute path")
    resolved = path.resolve()
    root = recordings_path().resolve()
    if resolved == root or root in resolved.parents or resolved in root.parents or resolved == Path("/"):
        raise HTTPException(status_code=422, detail="The cold tier's folder must be outside the recordings folder (and not contain it)")
    return str(resolved)


def _fields(body: TierInput, current=None) -> dict[str, Any]:
    fields: dict[str, Any] = {"name": body.name, "kind": body.kind, "allow_private": int(body.allow_private), "s3_region": body.s3_region, "s3_prefix": body.s3_prefix,
                              "local_path": None, "s3_endpoint": None, "s3_bucket": None}
    if body.kind == "local":
        fields["local_path"] = _local_path(body.local_path or "")
    else:
        fields["s3_endpoint"] = check_endpoint(body.s3_endpoint or "", body.allow_private)
        fields["s3_bucket"] = body.s3_bucket
    return fields


def _probe(tier_id: str) -> None:
    """Prove the tier works with the saved settings; raises 422 with the reason when it does not."""
    with connect() as db:
        tier = tiering.load_tier(db, tier_id)
    store = tiering.open_store(tier)
    try:
        store.check()
    except (OSError, S3Error) as exc:
        raise HTTPException(status_code=422, detail=f"The cold tier does not work: {exc}") from None
    finally:
        tiering._close(store)


@router.post("/api/admin/tiering/tiers", status_code=status.HTTP_201_CREATED)
async def create_tier(body: TierInput, user: Administrator) -> dict[str, Any]:
    if body.kind == "s3" and not (body.access_key and body.secret_key):
        raise HTTPException(status_code=422, detail="Enter the access key and the secret key")
    fields = _fields(body)
    tier_id = str(uuid.uuid4())
    with connect() as db:
        if db.execute("SELECT COUNT(*) FROM cold_tiers").fetchone()[0] >= MAX_TIERS:
            raise HTTPException(status_code=422, detail=f"At most {MAX_TIERS} cold tiers")
        if db.execute("SELECT 1 FROM cold_tiers WHERE name=?", (fields["name"],)).fetchone():
            raise HTTPException(status_code=409, detail="A cold tier with this name already exists")
        sealed = tiering.seal_credentials(body.access_key, body.secret_key) if body.access_key and body.secret_key else None
        columns = ",".join(fields)
        db.execute(f"INSERT INTO cold_tiers(id,{columns},credentials_ciphertext,created_by) VALUES(?,{','.join('?' * len(fields))},?,?)", (tier_id, *fields.values(), sealed, int(user["id"])))
    try:
        await run_in_threadpool(_probe, tier_id)
    except HTTPException:
        with connect() as db:
            db.execute("DELETE FROM cold_tiers WHERE id=?", (tier_id,))  # Not kept unless it works
        raise
    with connect() as db:
        db.execute("UPDATE cold_tiers SET last_checked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (tier_id,))
        record(db, int(user["id"]), "cold_tier_created", "cold_tier", tier_id, name=fields["name"], kind=fields["kind"])
        return _view(db)


@router.put("/api/admin/tiering/tiers/{tier_id}")
async def update_tier(tier_id: str, body: TierInput, user: Administrator) -> dict[str, Any]:
    fields = _fields(body)
    with connect() as db:
        current = db.execute("SELECT * FROM cold_tiers WHERE id=?", (tier_id,)).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="Cold tier not found")
        if current["kind"] != body.kind and db.execute("SELECT 1 FROM recording_segments WHERE tier_id=? LIMIT 1", (tier_id,)).fetchone():
            raise HTTPException(status_code=409, detail="Footage is stored on this tier: its kind cannot change")
        if (body.kind == "local" and fields["local_path"] != current["local_path"] or body.kind == "s3" and (fields["s3_bucket"] != current["s3_bucket"] or fields["s3_prefix"] != current["s3_prefix"]
                                                                                                         or fields["s3_endpoint"] != current["s3_endpoint"])) \
                and db.execute("SELECT 1 FROM recording_segments WHERE tier_id=? LIMIT 1", (tier_id,)).fetchone():
            raise HTTPException(status_code=409, detail="Footage is stored on this tier: its location cannot change (move the footage first)")
        if db.execute("SELECT 1 FROM cold_tiers WHERE name=? AND id<>?", (fields["name"], tier_id)).fetchone():
            raise HTTPException(status_code=409, detail="A cold tier with this name already exists")
        sets = ",".join(f"{name}=?" for name in fields)
        db.execute(f"UPDATE cold_tiers SET {sets} WHERE id=?", (*fields.values(), tier_id))
        if body.access_key and body.secret_key:
            db.execute("UPDATE cold_tiers SET credentials_ciphertext=? WHERE id=?", (tiering.seal_credentials(body.access_key, body.secret_key), tier_id))
        elif body.kind == "s3" and current["credentials_ciphertext"] is None:
            raise HTTPException(status_code=422, detail="Enter the access key and the secret key")
    await run_in_threadpool(_probe, tier_id)
    with connect() as db:
        db.execute("UPDATE cold_tiers SET last_checked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),last_error=NULL,failures=0 WHERE id=?", (tier_id,))
        record(db, int(user["id"]), "cold_tier_updated", "cold_tier", tier_id, name=fields["name"], credentials_replaced=bool(body.access_key))
        return _view(db)


@router.post("/api/admin/tiering/tiers/{tier_id}/check")
async def check_tier(tier_id: str, _: Administrator) -> dict[str, Any]:
    with connect() as db:
        if db.execute("SELECT 1 FROM cold_tiers WHERE id=?", (tier_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Cold tier not found")
    try:
        await run_in_threadpool(_probe, tier_id)
    except HTTPException as exc:
        await run_in_threadpool(tiering.note_health, tier_id, str(exc.detail))
        raise
    await run_in_threadpool(tiering.note_health, tier_id, None)
    with connect() as db:
        return _view(db)


@router.delete("/api/admin/tiering/tiers/{tier_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tier(tier_id: str, user: Administrator) -> Response:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT name FROM cold_tiers WHERE id=?", (tier_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Cold tier not found")
            if db.execute("SELECT 1 FROM tier_policies WHERE tier_id=?", (tier_id,)).fetchone():
                raise HTTPException(status_code=409, detail="A policy sends footage to this tier: remove the policy first")
            if db.execute("SELECT 1 FROM recording_segments WHERE tier_id=? LIMIT 1", (tier_id,)).fetchone() or db.execute("SELECT 1 FROM tier_moves WHERE tier_id=? LIMIT 1", (tier_id,)).fetchone():
                raise HTTPException(status_code=409, detail="Footage is stored on this tier: it stays until retention removes it")
            db.execute("DELETE FROM cold_tiers WHERE id=?", (tier_id,))
            record(db, int(user["id"]), "cold_tier_deleted", "cold_tier", tier_id, name=row["name"])
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/api/admin/tiering/policies/{volume_id}")
def put_policy(volume_id: str, body: PolicyInput, user: Administrator) -> dict[str, Any]:
    if volume_id not in configured_volumes():
        raise HTTPException(status_code=404, detail="Storage volume not found")
    with connect() as db:
        if db.execute("SELECT 1 FROM cold_tiers WHERE id=?", (body.tier_id,)).fetchone() is None:
            raise HTTPException(status_code=422, detail="Unknown cold tier")
        db.execute("INSERT INTO tier_policies(volume_id,tier_id,after_days,rate_kbps,enabled) VALUES(?,?,?,?,?) ON CONFLICT(volume_id) DO UPDATE SET tier_id=excluded.tier_id,"
                   "after_days=excluded.after_days,rate_kbps=excluded.rate_kbps,enabled=excluded.enabled,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                   (volume_id, body.tier_id, body.after_days, body.rate_kbps, int(body.enabled)))
        record(db, int(user["id"]), "tier_policy_changed", "storage_volume", volume_id, tier_id=body.tier_id, after_days=body.after_days, enabled=body.enabled)
        return _view(db)


@router.delete("/api/admin/tiering/policies/{volume_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_policy(volume_id: str, user: Administrator) -> Response:
    with connect() as db:
        if db.execute("DELETE FROM tier_policies WHERE volume_id=?", (volume_id,)).rowcount == 0:
            raise HTTPException(status_code=404, detail="No policy for this volume")
        record(db, int(user["id"]), "tier_policy_removed", "storage_volume", volume_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/admin/tiering/run")
async def run_now(_: Administrator) -> dict[str, Any]:
    """Move up to five of each policy's oldest eligible segments now, instead of waiting for the next pass."""
    outcomes = await run_in_threadpool(tiering.run_once, None, 5)
    with connect() as db:
        return {"moved": outcomes.get("moved", 0), "outcomes": outcomes, **_view(db)}
