"""Validated process entrypoint for the configurable host-network listener."""
from __future__ import annotations

import os

import uvicorn


def listen_port() -> int:
    raw = os.environ.get("NVR_LISTEN_PORT", "8280")
    try:
        port = int(raw)
    except ValueError as exc:
        raise SystemExit("NVR_LISTEN_PORT must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise SystemExit("NVR_LISTEN_PORT must be an integer from 1 to 65535")
    return port


def main() -> None:
    uvicorn.run("app.main:app", host="0.0.0.0", port=listen_port())


if __name__ == "__main__":
    main()
