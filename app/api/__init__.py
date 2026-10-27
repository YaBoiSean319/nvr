"""Domain routers, registered by the application factory in this order."""
from __future__ import annotations

from fastapi import APIRouter

from . import access_grants, ai_settings, alarms, alerts, automation, backups, cameras, coverage, detections, export_jobs, footage, identity, installable, mfa, live_alarms, footage_chain, live_media, privacy, ptz, live_views, onboarding, operations, playback, policies, push, rules, site_settings, system, tamper, viewing_audit, scrape, disks, hardening, camera_inspect, capabilities

ROUTERS: tuple[APIRouter, ...] = (
    system.router,
    installable.router,
    identity.router,
    mfa.router,
    access_grants.router,
    tamper.router,
    viewing_audit.router,
    scrape.router,
    disks.router,
    hardening.router,
    camera_inspect.router,
    automation.router,
    site_settings.router,
    live_views.router,
    policies.router,
    cameras.router,
    capabilities.router,
    onboarding.router,
    ai_settings.router,
    live_media.router,
    footage.router,
    detections.router,
    rules.router,
    alarms.router,
    push.router,
    live_alarms.router,
    ptz.router,
    privacy.router,
    footage_chain.router,
    coverage.router,
    playback.router,
    export_jobs.router,
    alerts.router,
    operations.router,
    backups.router,
)
