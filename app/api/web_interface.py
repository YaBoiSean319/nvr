"""The browser interface: one built page for every client route, plus its hashed assets.

Built assets live in app/web_dist (produced by the web/ Vite project in the container
build). Hashed assets are cached for a year; the page itself is always revalidated.
The router is registered after every API router, so its catch-all never shadows them;
unknown /api paths still answer with JSON 404s. The former /next preview paths redirect.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
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
# Paths owned by the server, never by client routing.
SERVER_PREFIXES = ("api/", "assets/")


def _page() -> FileResponse:
    page = DIST_DIRECTORY / "index.html"
    if not page.is_file():
        raise HTTPException(status_code=503, detail="The web interface has not been built")
    return FileResponse(page, media_type="text/html", headers={**SECURITY_HEADERS, "Cache-Control": "no-cache"})


@router.get("/next", include_in_schema=False)
@router.get("/next/{path:path}", include_in_schema=False)
def preview_redirect(request: Request, path: str = "") -> RedirectResponse:
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/{path}{query}", status_code=308)


@router.get("/assets/{path:path}", include_in_schema=False)
def asset(path: str) -> FileResponse:
    candidate = (DIST_DIRECTORY / "assets" / path).resolve()
    if (DIST_DIRECTORY / "assets").resolve() not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(candidate, headers={**SECURITY_HEADERS, "Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/", include_in_schema=False)
@router.get("/{path:path}", include_in_schema=False)
def interface(path: str = "") -> FileResponse:
    if path.startswith(SERVER_PREFIXES):
        raise HTTPException(status_code=404, detail="Not found")
    return _page()
