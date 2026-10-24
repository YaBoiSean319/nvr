"""The installable app: a web app manifest and square icons drawn from site branding.

Both are public, like the sign-in page's branding, because browsers fetch them
before anyone signs in. Icon URLs carry a version derived from the branding, so an
installed app picks up a new name, colour or icon on its next load.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
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


def _branding() -> tuple[str, str, str, bytes | None, str]:
    with connect() as db:
        row = db.execute("SELECT app_name,primary_color,accent_color FROM site_branding WHERE singleton_id=1").fetchone()
        icon = db.execute("SELECT image_bytes,sha256 FROM site_branding_assets WHERE asset_key='icon'").fetchone()
    image, image_digest = (icon["image_bytes"], icon["sha256"]) if icon else (None, "default")
    version = hashlib.sha256(f"{image_digest}|{row['primary_color']}|{row['accent_color']}".encode()).hexdigest()[:16]
    return row["app_name"], row["primary_color"], row["accent_color"], image, version


@lru_cache(maxsize=16)
def _render(version: str, size: int, maskable: bool, primary: str, accent: str, image: bytes | None) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is unavailable")
    background = primary.replace("#", "0x")
    if image is None:
        # No uploaded icon: an accent square on the primary colour.
        inset = round(size * (0.3 if not maskable else 0.34))
        command = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"color=c={background}:s={size}x{size}",
                   "-vf", f"drawbox=x={inset}:y={inset}:w={size - 2 * inset}:h={size - 2 * inset}:color={accent.replace('#', '0x')}:t=fill"]
        stdin = None
    else:
        artwork = round(size * (MASKABLE_SAFE_ZONE if maskable else 1))
        command = [ffmpeg, "-v", "error", "-i", "pipe:0",
                   "-vf", f"scale={artwork}:{artwork}:force_original_aspect_ratio=decrease,pad={size}:{size}:(ow-iw)/2:(oh-ih)/2:color={background}"]
        stdin = image
    command += ["-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
    result = subprocess.run(command, input=stdin, capture_output=True, timeout=10, check=False)
    if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
        raise RuntimeError("The app icon could not be drawn")
    return result.stdout


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> JSONResponse:
    name, primary, _, _, version = _branding()
    icons = [{"src": f"/api/public/app-icons/{purpose}-{size}.png?v={version}", "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}
             for purpose in ("any", "maskable") for size in ICON_SIZES]
    body = {
        "name": name, "short_name": name[:SHORT_NAME_LENGTH].rstrip(), "id": "/", "start_url": "/", "scope": "/",
        "display": "standalone", "theme_color": primary, "background_color": primary, "icons": icons,
    }
    return JSONResponse(body, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@router.get("/api/public/app-icons/{purpose}-{size}.png", include_in_schema=False)
def app_icon(purpose: Literal["any", "maskable"], size: int) -> Response:
    if size not in ICON_SIZES:
        raise HTTPException(status_code=404, detail="Icon size not available")
    _, primary, accent, image, version = _branding()
    try:
        png = _render(version, size, purpose == "maskable", primary, accent, image)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=503, detail="The app icon is unavailable") from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400", "ETag": f'"{version}-{purpose}-{size}"'})
