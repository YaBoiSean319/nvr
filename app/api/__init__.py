"""Domain routers, registered by the application factory in this order."""
from __future__ import annotations

from fastapi import APIRouter

from . import ai_settings, alarms, alerts, automation, backups, cameras, detections, export_jobs, footage, identity, installable, live_media, live_views, onboarding, operations, playback, policies, rules, site_settings, system

ROUTERS: tuple[APIRouter, ...] = (
    system.router,
    installable.router,
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
    detections.router,
    rules.router,
    alarms.router,
    playback.router,
    export_jobs.router,
    alerts.router,
    operations.router,
    backups.router,
)
