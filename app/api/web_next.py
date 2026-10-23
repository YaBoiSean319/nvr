"""The rebuilt browser interface, served at /next while it replaces the legacy one.

Built assets live in app/web_dist (produced by the web/ Vite project in the
container build). Hashed assets are cached for a year; the page itself is always
revalidated. Unknown paths under /next return the page so client routes reload.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter()
DIST_DIRECTORY = Path(__file__).resolve().parent.parent / "web_dist"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def _page() -> FileResponse:
    page = DIST_DIRECTORY / "index.html"
    if not page.is_file():
        raise HTTPException(status_code=503, detail="The new interface has not been built")
    return FileResponse(page, media_type="text/html", headers={**SECURITY_HEADERS, "Cache-Control": "no-cache"})


@router.get("/next", include_in_schema=False)
def next_root() -> RedirectResponse:
    return RedirectResponse("/next/", status_code=307)


@router.get("/next/{path:path}", include_in_schema=False)
def next_interface(path: str) -> FileResponse:
    if path.startswith("assets/"):
        candidate = (DIST_DIRECTORY / path).resolve()
        if DIST_DIRECTORY.resolve() not in candidate.parents or not candidate.is_file():
            raise HTTPException(status_code=404, detail="Asset not found")
        return FileResponse(candidate, headers={**SECURITY_HEADERS, "Cache-Control": "public, max-age=31536000, immutable"})
    return _page()
