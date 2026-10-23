"""Domain routers, registered by the application factory in this order."""
from __future__ import annotations

from fastapi import APIRouter

from . import ai_settings, cameras, identity, live_views, onboarding, policies, site_settings, system

ROUTERS: tuple[APIRouter, ...] = (
    system.router,
    identity.router,
    site_settings.router,
    live_views.router,
    policies.router,
    cameras.router,
    onboarding.router,
    ai_settings.router,
)
