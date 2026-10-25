"""Shared lifecycle for worker processes: signals, control socket and heartbeat."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Protocol

from app import logforward
from app.workers import control, liveness


class Service(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def handle(self, message: dict[str, object]) -> None: ...


async def run(name: str, service: Service) -> None:
    os.umask(0o077)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    await service.start()
    server = await control.serve(name, service.handle)
    heartbeat = asyncio.create_task(liveness.heartbeat_forever(name), name=f"{name}-heartbeat")
    try:
        await stop.wait()
    finally:
        server.close()
        heartbeat.cancel()
        await service.stop()


def main(name: str, service: Service) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logforward.install(name)
    asyncio.run(run(name, service))
