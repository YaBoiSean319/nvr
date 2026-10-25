"""The web worker: the HTTP API and browser interface."""
from __future__ import annotations

import uvicorn

from app import logforward
from app.entrypoint import listen_port


def main() -> None:
    logforward.install("web")
    uvicorn.run("app.main:app", host="0.0.0.0", port=listen_port(), timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
