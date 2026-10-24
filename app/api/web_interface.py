"""The browser interface: one built page for every client route, plus its hashed assets.

Built assets live in app/web_dist (produced by the web/ Vite project in the container
build). Hashed assets are cached for a year; the page itself is always revalidated.
The router is registered after every API router, so its catch-all never shadows them;
unknown /api paths still answer with JSON 404s. The former /next preview paths redirect.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

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
SERVER_PREFIXES = ("api/", "assets/", "sw.js", "manifest.webmanifest")


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


# The service worker caches only the application shell and hashed assets. It never
# intercepts API, media or snapshot requests. Its version is the built page's digest, so
# every release installs a new worker that deletes the previous release's caches.
SERVICE_WORKER = """'use strict';
const VERSION = "__VERSION__";
const SHELL = `nvr-shell-${VERSION}`;

self.addEventListener('install', event => {
  event.waitUntil(caches.open(SHELL).then(cache => cache.add(new Request('/', { cache: 'reload' }))).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(key => key.startsWith('nvr-shell-') && key !== SHELL).map(key => caches.delete(key))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/assets/')) {
    // Hashed file names never change content, so the first copy is the right one.
    event.respondWith(caches.open(SHELL).then(cache => cache.match(request).then(hit => hit || fetch(request).then(response => {
      if (response.ok && response.type === 'basic') cache.put(request, response.clone());
      return response;
    }))));
    return;
  }
  if (request.mode === 'navigate' && !url.pathname.startsWith('/api/')) {
    // The page always comes from the network when it can, so a new release shows at once.
    event.respondWith(fetch(request).then(response => {
      if (response.ok && response.type === 'basic') caches.open(SHELL).then(cache => cache.put('/', response.clone()));
      return response;
    }).catch(() => caches.open(SHELL).then(cache => cache.match('/')).then(hit => hit || Response.error())));
  }
  // Everything else (API responses, live and recorded media, snapshots) is never touched.
});
"""


@router.get("/sw.js", include_in_schema=False)
def service_worker() -> Response:
    page = DIST_DIRECTORY / "index.html"
    if not page.is_file():
        raise HTTPException(status_code=503, detail="The web interface has not been built")
    version = hashlib.sha256(page.read_bytes()).hexdigest()[:16]
    return Response(SERVICE_WORKER.replace("__VERSION__", version), media_type="text/javascript; charset=utf-8",
                    headers={**SECURITY_HEADERS, "Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


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
