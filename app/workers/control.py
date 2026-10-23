"""Best-effort nudges from the web process to workers over a local Unix socket.

A nudge only shortens the time until a worker acts on configuration that is
already committed to the database. Losing one is harmless: each worker also
reconciles from the database on its own schedule.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

from app.workers import control_directory

logger = logging.getLogger("nvr.workers.control")
MAX_MESSAGE_BYTES = 4096

Handler = Callable[[dict[str, object]], Awaitable[None]]


def socket_path(worker: str) -> Path:
    return control_directory() / f"{worker}.sock"


async def nudge(worker: str, message: dict[str, object], connect_timeout: float = 0.5, completion_timeout: float = 10.0) -> bool:
    """Ask a worker to act now; True once it reports having done so.

    Waiting for completion keeps the in-process ordering callers relied on (for
    example, recording has stopped before an archive request returns) while a
    worker that is down or slow never blocks the caller for long.
    """
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(socket_path(worker))), connect_timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        writer.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        await asyncio.wait_for(writer.drain(), connect_timeout)
        return await asyncio.wait_for(reader.readline(), completion_timeout) == b"ok\n"
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def serve(worker: str, handler: Handler) -> asyncio.AbstractServer:
    path = socket_path(worker)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.unlink(missing_ok=True)

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 2)
            if line and len(line) <= MAX_MESSAGE_BYTES:
                message = json.loads(line)
                if isinstance(message, dict):
                    await handler(message)
                    writer.write(b"ok\n")
                    await writer.drain()
        except (asyncio.TimeoutError, ValueError, OSError):
            pass
        except Exception:  # noqa: BLE001 - a bad nudge must never stop the worker
            logger.exception("Control message for %s failed", worker)
        finally:
            writer.close()

    server = await asyncio.start_unix_server(connection, path=str(path))
    os.chmod(path, 0o600)
    return server


class RecorderClient:
    """Stands in for the in-process recorder inside the supervised web process."""

    async def sync_camera(self, camera_id: str, force_restart: bool = False) -> None:
        await nudge("recorder", {"op": "sync_camera", "camera_id": camera_id, "force_restart": force_restart})
