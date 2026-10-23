"""Container entrypoint: validates the listener and runs the worker supervisor."""
from __future__ import annotations

import os


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
    listen_port()
    from app.workers.supervisor import main as supervise

    raise SystemExit(supervise())


if __name__ == "__main__":
    main()
