"""The installable app: a web app manifest and square icons drawn from site branding.

Both are public, like the sign-in page's branding, because browsers fetch them
before anyone signs in. Icon URLs carry a version derived from the branding, so an
installed app picks up a new name, colour or icon on its next load.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from app.store import connect

router = APIRouter()

ICON_SIZES = (192, 512)
SHORT_NAME_LENGTH = 12
# Maskable icons keep their artwork inside the central safe zone (80% of the canvas).
MASKABLE_SAFE_ZONE = 0.8


@dataclass(frozen=True)
class _Branding:
    name: str
    primary: str
    accent: str
    # Changes whenever the icon or a colour changes; icon URLs and renders are keyed on it.
    version: str


def _branding() -> _Branding:
    with connect() as db:
        row = db.execute("SELECT app_name,primary_color,accent_color FROM site_branding WHERE singleton_id=1").fetchone()
        icon = db.execute("SELECT sha256 FROM site_branding_assets WHERE asset_key='icon'").fetchone()
    digest = icon["sha256"] if icon else "default"
    version = hashlib.sha256(f"{digest}|{row['primary_color']}|{row['accent_color']}".encode()).hexdigest()[:16]
    return _Branding(row["app_name"], row["primary_color"], row["accent_color"], version)


def _icon_image() -> bytes | None:
    with connect() as db:
        icon = db.execute("SELECT image_bytes FROM site_branding_assets WHERE asset_key='icon'").fetchone()
    return icon["image_bytes"] if icon else None


@lru_cache(maxsize=32)
def _render(version: str, size: int, maskable: bool, primary: str, accent: str) -> bytes | None:
    """The icon PNG, or None when it cannot be drawn (remembered, so failures cost one attempt)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    background = primary.replace("#", "0x")
    image = _icon_image()
    if image is None:
        # No uploaded icon: an accent square on the primary colour.
        inset = round(size * (0.3 if not maskable else 0.34))
        command = [ffmpeg, "-v", "error", "-threads", "1", "-f", "lavfi", "-i", f"color=c={background}:s={size}x{size}",
                   "-vf", f"drawbox=x={inset}:y={inset}:w={size - 2 * inset}:h={size - 2 * inset}:color={accent.replace('#', '0x')}:t=fill"]
    else:
        artwork = round(size * (MASKABLE_SAFE_ZONE if maskable else 1))
        command = [ffmpeg, "-v", "error", "-threads", "1", "-max_pixels", "16777216", "-i", "pipe:0",
                   "-vf", f"scale={artwork}:{artwork}:force_original_aspect_ratio=decrease,pad={size}:{size}:(ow-iw)/2:(oh-ih)/2:color={background}"]
    command += ["-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
    try:
        result = subprocess.run(command, input=image, capture_output=True, timeout=10, check=False)
    except subprocess.TimeoutExpired:
        return None
    return result.stdout if result.returncode == 0 and result.stdout.startswith(b"\x89PNG") else None


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> JSONResponse:
    branding = _branding()
    name = branding.name
    icons = [{"src": f"/api/public/app-icons/{purpose}-{size}.png?v={branding.version}", "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}
             for purpose in ("any", "maskable") for size in ICON_SIZES]
    body = {
        "name": name, "short_name": name[:SHORT_NAME_LENGTH].rstrip(), "id": "/", "start_url": "/", "scope": "/",
        "display": "standalone", "theme_color": branding.primary, "background_color": branding.primary, "icons": icons,
    }
    return JSONResponse(body, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@router.get("/api/public/app-icons/{purpose}-{size}.png", include_in_schema=False)
def app_icon(purpose: Literal["any", "maskable"], size: int, v: str | None = None) -> Response:
    if size not in ICON_SIZES:
        raise HTTPException(status_code=404, detail="Icon size not available")
    branding = _branding()
    png = _render(branding.version, size, purpose == "maskable", branding.primary, branding.accent)
    if png is None:
        raise HTTPException(status_code=503, detail="The app icon is unavailable")
    # A versioned address never changes content; an unversioned one follows branding.
    caching = "public, max-age=31536000, immutable" if v == branding.version else "no-cache"
    return Response(png, media_type="image/png", headers={"Cache-Control": caching})
