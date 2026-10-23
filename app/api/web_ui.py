"""The browser interface's static page and its allow-listed scripts and stylesheets."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()
STATIC_DIRECTORY = Path(__file__).resolve().parent.parent / "static"


@router.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIRECTORY / "index.html")


STATIC_JAVASCRIPT_ASSETS = frozenset({
    "app.js",
    "workspace-layout.js",
    "camera-inventory.js",
    "camera-editor.js",
    "camera-admin-save.js",
    "camera-discovery.js",
    "camera-onboarding.js",
    "camera-preview.js",
    "camera-preview-ui.js",
    "camera-bulk-actions.js",
    "camera-groups.js",
})


STATIC_STYLESHEET_ASSETS = frozenset({"camera-groups.css"})


@router.get("/{asset_name}.js", include_in_schema=False)
def javascript_asset(asset_name: str) -> FileResponse:
    filename = f"{asset_name}.js"
    if filename not in STATIC_JAVASCRIPT_ASSETS:
        raise HTTPException(status_code=404, detail="JavaScript asset not found")
    path = STATIC_DIRECTORY / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="JavaScript asset not found")
    return FileResponse(path, media_type="text/javascript")


@router.get("/{asset_name}.css", include_in_schema=False)
def stylesheet_asset(asset_name: str) -> FileResponse:
    filename = f"{asset_name}.css"
    if filename not in STATIC_STYLESHEET_ASSETS:
        raise HTTPException(status_code=404, detail="Stylesheet asset not found")
    path = STATIC_DIRECTORY / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Stylesheet asset not found")
    return FileResponse(path, media_type="text/css")
