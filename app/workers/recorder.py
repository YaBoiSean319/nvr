"""The recorder worker: recording, schedules, retention, storage health and motion."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from app.recording import RecordingManager
from app.workers import control, liveness

logger = logging.getLogger("nvr.workers.recorder")


async def run() -> None:
    os.umask(0o077)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)

    manager = RecordingManager()
    await manager.start()

    async def handle(message: dict[str, object]) -> None:
        if message.get("op") == "sync_camera" and isinstance(message.get("camera_id"), str):
            await manager.sync_camera(message["camera_id"], force_restart=bool(message.get("force_restart")))

    server = await control.serve("recorder", handle)
    heartbeat = asyncio.create_task(liveness.heartbeat_forever("recorder"), name="recorder-heartbeat")
    try:
        await stop.wait()
    finally:
        server.close()
        heartbeat.cancel()
        await manager.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
