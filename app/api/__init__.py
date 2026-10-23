"""Domain routers, registered by the application factory in this order."""
from __future__ import annotations

from fastapi import APIRouter

from . import ai_settings, alerts, automation, cameras, export_jobs, footage, identity, live_media, live_views, onboarding, operations, playback, policies, site_settings, system, web_ui

ROUTERS: tuple[APIRouter, ...] = (
    system.router,
    identity.router,
    automation.router,
    site_settings.router,
    live_views.router,
    policies.router,
    cameras.router,
    onboarding.router,
    ai_settings.router,
    live_media.router,
    footage.router,
    playback.router,
    export_jobs.router,
    alerts.router,
    operations.router,
    # Last: its catch-all asset paths must not shadow API routes.
    web_ui.router,
)
