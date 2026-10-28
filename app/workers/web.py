"""The web worker: the HTTP API and browser interface."""
from __future__ import annotations

import copy

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from app import logforward
from app.entrypoint import listen_port


def log_config() -> dict:
    """Uvicorn's own logging, with its server and error log (start-up failures, "Exception in ASGI application") also passed up to the
    root logger, where log forwarding listens. The access log, one line per request, stays out of it."""
    config = copy.deepcopy(LOGGING_CONFIG)
    config["loggers"]["uvicorn"]["propagate"] = True
    return config


def main() -> None:
    logforward.install("web")
    # Forwarded headers are believed only from NVR_TRUSTED_PROXIES (app.network), never by uvicorn on its own: its default trusts any 127.0.0.1 sender.
    uvicorn.run("app.main:app", host="0.0.0.0", port=listen_port(), timeout_graceful_shutdown=10, log_config=log_config(), proxy_headers=False)


if __name__ == "__main__":
    main()
