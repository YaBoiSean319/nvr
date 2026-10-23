"""Application factory: one place that assembles middleware, routers and lifecycle."""
from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware

from . import api
from .detection import DetectionManager
from .exports import ExportManager
from .heartbeat import HeartbeatManager
from .recording import RecordingManager
from .principal import secure_cookie
from .store import initialize
from .version import APP_VERSION
from .webhooks import WebhookManager


STARTUP_ORDER = (
    ("exports", ExportManager),
    ("webhooks", WebhookManager),
    ("heartbeat", HeartbeatManager),
    ("recorders", RecordingManager),
    ("detectors", DetectionManager),
)
# Detection reads recorder state, and recording should outlive everything else.
SHUTDOWN_ORDER = ("detectors", "exports", "webhooks", "heartbeat", "recorders")


def create_app(*, start_workers: bool = True) -> FastAPI:
    """Build the web application.

    Configuration (database, recordings, key and identity settings) comes from the
    environment, so tests isolate an instance by pointing those variables at
    temporary paths before entering its lifespan. ``start_workers=False`` serves the
    API without recording, export, webhook, heartbeat or detection loops.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        os.umask(0o077)
        initialize()
        started = {}
        try:
            if start_workers:
                for name, factory in STARTUP_ORDER:
                    manager = factory()
                    setattr(app.state, name, manager)
                    await manager.start()
                    started[name] = manager
            yield
        finally:
            for name in SHUTDOWN_ORDER:
                if name in started:
                    await started[name].stop()

    app = FastAPI(title="Workplace NVR", version=APP_VERSION, lifespan=lifespan)

    @app.middleware("http")
    async def prevent_api_caching(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=os.environ.get("NVR_OIDC_SESSION_SECRET") or secrets.token_urlsafe(48),
        same_site="lax",
        https_only=secure_cookie(),
        session_cookie="nvr_oidc_flow",
        max_age=600,
    )
    for router in api.ROUTERS:
        app.include_router(router)
    return app
