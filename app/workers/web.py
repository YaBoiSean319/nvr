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
    uvicorn.run("app.main:app", host="0.0.0.0", port=listen_port(), timeout_graceful_shutdown=10, log_config=log_config())


if __name__ == "__main__":
    main()
