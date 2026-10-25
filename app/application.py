"""Application factory: one place that assembles middleware, routers and lifecycle."""
from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from starlette.middleware.sessions import SessionMiddleware

from . import api
from .api import web_interface
from .backup import BackupManager
from .detection import DetectionManager
from .live_hub import LiveHub
from .exports import ExportManager
from .heartbeat import HeartbeatManager
from .recording import RecordingManager
from .push import PushManager
from .footage_chain import FootageChainManager
from .hardening import HardeningManager
from .self_test import SelfTestManager
from .ptz import PtzTourManager
from .rule_actions import RuleActionManager
from .rules import RulesManager
from .principal import COOKIE_NAME, current_user, depends_on, scope_for, secure_cookie
from .store import initialize
from .version import APP_VERSION
from .webhooks import WebhookManager
from .workers import liveness, supervised
from .workers.control import BackupClient, DetectionClient, ExportClient, RecorderClient, RulesClient


STARTUP_ORDER = (
    ("exports", ExportManager),
    ("webhooks", WebhookManager),
    ("push", PushManager),
    ("rule_actions", RuleActionManager),
    ("ptz_tours", PtzTourManager),
    ("footage_chain", FootageChainManager),
    ("self_test", SelfTestManager),
    ("hardening", HardeningManager),
    ("heartbeat", HeartbeatManager),
    ("recorders", RecordingManager),
    ("detectors", DetectionManager),
    ("rules", RulesManager),
    ("backups", BackupManager),
)
# Detection reads recorder state, and recording should outlive everything else.
SHUTDOWN_ORDER = ("backups", "rules", "detectors", "exports", "hardening", "self_test", "footage_chain", "ptz_tours", "rule_actions", "push", "webhooks", "heartbeat", "recorders")


def create_app(*, start_workers: bool = True) -> FastAPI:
    """Build the web application.

    Configuration (database, recordings, key and identity settings) comes from the
    environment, so tests isolate an instance by pointing those variables at
    temporary paths before entering its lifespan. ``start_workers=False`` serves the
    API without recording, export, webhook, heartbeat, detection or rules loops. Under the
    supervisor (``NVR_SUPERVISED=1``) all of that runs in separate worker
    processes and the web process reaches them through control-channel clients.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        os.umask(0o077)
        initialize()
        started = {}
        heartbeat = None
        app.state.live_hub = LiveHub()
        try:
            if start_workers and supervised():
                # Background work runs in supervised worker processes; the web
                # process only nudges them.
                app.state.recorders = RecorderClient()
                app.state.exports = ExportClient()
                app.state.detectors = DetectionClient()
                app.state.rules = RulesClient()
                app.state.backups = BackupClient()
            elif start_workers:
                for name, factory in STARTUP_ORDER:
                    manager = factory()
                    setattr(app.state, name, manager)
                    await manager.start()
                    started[name] = manager
            if supervised():
                heartbeat = asyncio.create_task(liveness.heartbeat_forever("web"), name="web-heartbeat")
            yield
        finally:
            await app.state.live_hub.close()
            if heartbeat:
                heartbeat.cancel()
            for name in SHUTDOWN_ORDER:
                if name in started:
                    await started[name].stop()

    app = FastAPI(title="Workplace NVR", version=APP_VERSION, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_failed(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default echoes the submitted value ("input"), which may be a password,
        # an action's secret URL or a token. Say what is wrong and where, never what was sent.
        detail = [{key: error[key] for key in ("type", "loc", "msg") if key in error} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(detail)})

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
    # Last: the interface's client-route catch-all must never shadow an API route.
    app.include_router(web_interface.router)
    app.openapi = lambda: _openapi_with_scopes(app)
    return app


def _openapi_with_scopes(app: FastAPI) -> dict[str, object]:
    """Publish how each operation is authorized: session cookie or a scoped API token."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schema.setdefault("components", {})["securitySchemes"] = {
        "session": {"type": "apiKey", "in": "cookie", "name": COOKIE_NAME},
        "apiToken": {
            "type": "http",
            "scheme": "bearer",
            "description": "Scoped API token created by an administrator. Scopes: read, media, export, metrics, admin (admin implies all).",
        },
    }
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema or not depends_on(route.dependant, current_user):
            continue
        for method in route.methods:
            operation = schema["paths"][route.path_format][method.lower()]
            scope = scope_for(route, method)
            operation["x-required-scope"] = scope
            operation["security"] = [{"session": []}, {"apiToken": [scope]}]
            note = f"Requires a signed-in session or an API token with the `{scope}` scope."
            operation["description"] = f"{operation['description']}\n\n{note}" if operation.get("description") else note
    app.openapi_schema = schema
    return schema
