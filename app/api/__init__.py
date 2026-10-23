"""Domain routers, registered by the application factory in this order."""
from __future__ import annotations

from fastapi import APIRouter

from . import identity, live_views, site_settings, system

ROUTERS: tuple[APIRouter, ...] = (
    system.router,
    identity.router,
    site_settings.router,
    live_views.router,
)
