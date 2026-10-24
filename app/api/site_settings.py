"""Site display settings and branding."""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import shutil
import subprocess
import sqlite3
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from app.principal import Administrator, CurrentUser
from app.store import connect
from app.display_settings import format_display_datetime, validate_display_settings

router = APIRouter()


class DisplaySettingsRequest(BaseModel):
    time_zone: str = Field(min_length=1, max_length=128)
    date_format: str = Field(min_length=1, max_length=32)
    clock_format: str = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_display_options(self) -> "DisplaySettingsRequest":
        try:
            validate_display_settings(self.time_zone.strip(), self.date_format, self.clock_format)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        self.time_zone = self.time_zone.strip()
        return self


class BrandingAssetRequest(BaseModel):
    media_type: Literal["image/png", "image/webp", "image/jpeg"]
    data_base64: str = Field(min_length=4, max_length=2_800_000)


class BrandingSaveRequest(BaseModel):
    app_name: str = Field(min_length=1, max_length=80)
    primary_color: str = Field(min_length=7, max_length=7)
    accent_color: str = Field(min_length=7, max_length=7)
    assets: dict[str, BrandingAssetRequest | None] = Field(default_factory=dict)
    reset_defaults: bool = False

    @field_validator("app_name")
    @classmethod
    def normalize_app_name(cls, value: str) -> str:
        value=value.strip()
        if not value or any(ord(char)<32 or ord(char)==127 for char in value):
            raise ValueError("Application name must be plain text between 1 and 80 characters")
        return value

    @field_validator("primary_color", "accent_color")
    @classmethod
    def validate_hex_color(cls, value: str) -> str:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("Use a six-digit hex color such as #1a2b3c")
        return value.lower()

    @model_validator(mode="after")
    def validate_asset_keys(self) -> "BrandingSaveRequest":
        if not set(self.assets) <= {"primary", "light", "dark", "icon"}:
            raise ValueError("Unknown branding asset key")
        if self.reset_defaults and self.assets:
            raise ValueError("Reset to defaults cannot be combined with asset changes")
        return self


def _display_settings_view(row: sqlite3.Row) -> dict[str, object]:
    return {"time_zone": row["time_zone"], "date_format": row["date_format"],
            "clock_format": row["clock_format"], "updated_at": row["updated_at"]}


@router.get("/api/settings/display")
def get_display_settings(_: CurrentUser) -> dict[str, object]:
    with connect() as db:
        row = db.execute("SELECT time_zone,date_format,clock_format,updated_at FROM site_display_settings WHERE singleton_id=1").fetchone()
    return _display_settings_view(row)


@router.post("/api/settings/display/preview")
def preview_display_settings(payload: DisplaySettingsRequest, _: Administrator) -> dict[str, object]:
    sample = datetime(2026, 10, 21, 16, 5, 6, tzinfo=timezone.utc)
    return {**payload.model_dump(), "preview": format_display_datetime(
        sample, payload.time_zone, payload.date_format, payload.clock_format
    )}


@router.put("/api/settings/display")
def save_display_settings(payload: DisplaySettingsRequest, _: Administrator) -> dict[str, object]:
    updated_at = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute(
            "UPDATE site_display_settings SET time_zone=?,date_format=?,clock_format=?,updated_at=? WHERE singleton_id=1",
            (payload.time_zone, payload.date_format, payload.clock_format, updated_at),
        )
        row = db.execute("SELECT time_zone,date_format,clock_format,updated_at FROM site_display_settings WHERE singleton_id=1").fetchone()
    return _display_settings_view(row)


BRANDING_DEFAULTS = {"app_name": "Workplace NVR", "primary_color": "#17212b", "accent_color": "#69a1ff"}


BRANDING_ASSET_TYPES = {"primary", "light", "dark", "icon"}


def _branding_view(db: sqlite3.Connection) -> dict[str, object]:
    row=db.execute("SELECT app_name,primary_color,accent_color,updated_at FROM site_branding WHERE singleton_id=1").fetchone()
    assets={}
    for asset in db.execute("SELECT asset_key,media_type,sha256,updated_at FROM site_branding_assets").fetchall():
        assets[asset["asset_key"]]={"url":f"/api/public/branding/assets/{asset['asset_key']}?v={asset['sha256'][:16]}",
                                     "media_type":asset["media_type"],"sha256":asset["sha256"]}
    return {"app_name":row["app_name"],"primary_color":row["primary_color"],"accent_color":row["accent_color"],
            "assets":assets,"updated_at":row["updated_at"]}


def _decode_branding_asset(asset: BrandingAssetRequest) -> tuple[bytes, str]:
    try:
        data=base64.b64decode(asset.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Branding image data is not valid base64") from exc
    if not data or len(data)>2*1024*1024:
        raise HTTPException(status_code=413, detail="Each branding image must be no larger than 2 MiB")
    signatures={"image/png":data.startswith(b"\x89PNG\r\n\x1a\n"),
                "image/jpeg":data.startswith(b"\xff\xd8\xff"),
                "image/webp":len(data)>=12 and data[:4]==b"RIFF" and data[8:12]==b"WEBP"}
    if not signatures.get(asset.media_type,False):
        raise HTTPException(status_code=415, detail="Image bytes do not match the declared PNG, WebP, or JPEG type")
    ffprobe,ffmpeg=shutil.which("ffprobe"),shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise HTTPException(status_code=503, detail="Image validation tools are unavailable; branding was not changed")
    stage="probe"
    try:
        probed=subprocess.run([ffprobe,"-v","error","-select_streams","v:0","-show_entries","stream=codec_name,width,height","-of","json","-i","pipe:0"],
                              input=data,capture_output=True,timeout=8,check=False)
        metadata=json.loads(probed.stdout) if probed.returncode==0 else {}
        streams=metadata.get("streams") or []
        if len(streams)!=1:
            raise ValueError("Image must contain exactly one frame stream")
        stream=streams[0];width=int(stream.get("width") or 0);height=int(stream.get("height") or 0)
        expected_codec={"image/png":"png","image/jpeg":"mjpeg","image/webp":"webp"}[asset.media_type]
        if stream.get("codec_name")!=expected_codec or not (1<=width<=4096 and 1<=height<=4096) or width*height>16_777_216:
            raise ValueError("Image type or dimensions are unsupported")
        stage="decode"
        decoded=subprocess.run([ffmpeg,"-hide_banner","-v","error","-threads","1","-max_pixels","16777216","-i","pipe:0","-frames:v","1","-f","null","-"],
                               input=data,capture_output=True,timeout=8,check=False)
        if decoded.returncode!=0:
            raise ValueError("Image could not be decoded")
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=422, detail=f"Image {stage} validation timed out") from exc
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return data,hashlib.sha256(data).hexdigest()


BRANDING_ASSET_LABELS = {"primary": "Primary logo", "light": "Light-theme logo", "dark": "Dark-theme logo", "icon": "Icon"}


def _decode_named_asset(key: str, asset: BrandingAssetRequest) -> tuple[bytes, str]:
    """Validate one upload, naming the asset in any error so the field can show it."""
    try:
        return _decode_branding_asset(asset)
    except HTTPException as exc:
        raise HTTPException(status_code=exc.status_code, detail=f"{BRANDING_ASSET_LABELS[key]}: {exc.detail}") from exc


@router.get("/api/public/branding")
def get_public_branding() -> dict[str, object]:
    with connect() as db:
        return _branding_view(db)


@router.get("/api/settings/branding")
def get_branding_settings(_: Administrator) -> dict[str, object]:
    with connect() as db:
        return _branding_view(db)


@router.put("/api/settings/branding")
def save_branding_settings(payload: BrandingSaveRequest, user: Administrator) -> dict[str, object]:
    validated_assets={key:None if asset is None else _decode_named_asset(key,asset) for key,asset in payload.assets.items()}
    now=datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if payload.reset_defaults:
                db.execute("UPDATE site_branding SET app_name=?,primary_color=?,accent_color=?,updated_by=?,updated_at=? WHERE singleton_id=1",
                           (BRANDING_DEFAULTS["app_name"],BRANDING_DEFAULTS["primary_color"],BRANDING_DEFAULTS["accent_color"],int(user["id"]),now))
                db.execute("DELETE FROM site_branding_assets")
                action="branding_reset"
                details={"assets":"all","fields":["app_name","primary_color","accent_color"]}
            else:
                db.execute("UPDATE site_branding SET app_name=?,primary_color=?,accent_color=?,updated_by=?,updated_at=? WHERE singleton_id=1",
                           (payload.app_name,payload.primary_color,payload.accent_color,int(user["id"]),now))
                for key,asset in validated_assets.items():
                    if asset is None:
                        db.execute("DELETE FROM site_branding_assets WHERE asset_key=?",(key,))
                    else:
                        data,digest=asset;requested=payload.assets[key]
                        db.execute("INSERT INTO site_branding_assets(asset_key,media_type,image_bytes,sha256,updated_at) VALUES(?,?,?,?,?) "
                                   "ON CONFLICT(asset_key) DO UPDATE SET media_type=excluded.media_type,image_bytes=excluded.image_bytes,sha256=excluded.sha256,updated_at=excluded.updated_at",
                                   (key,requested.media_type,data,digest,now))
                action="branding_saved"
                details={"fields":["app_name","primary_color","accent_color"],"asset_keys":sorted(validated_assets)}
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
                       (int(user["id"]),action,"site_branding","site",json.dumps(details,separators=(",",":"))))
            result=_branding_view(db)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


@router.get("/api/public/branding/assets/{asset_key}")
def get_public_branding_asset(asset_key: str) -> Response:
    if asset_key not in BRANDING_ASSET_TYPES:
        raise HTTPException(status_code=404, detail="Branding asset not found")
    with connect() as db:
        asset=db.execute("SELECT media_type,image_bytes,sha256 FROM site_branding_assets WHERE asset_key=?",(asset_key,)).fetchone()
    if asset is None:
        raise HTTPException(status_code=404, detail="Branding asset not found")
    return Response(content=asset["image_bytes"],media_type=asset["media_type"],headers={
        "ETag":f'"{asset["sha256"]}"',"Cache-Control":"public, max-age=31536000, immutable","X-Content-Type-Options":"nosniff"})
