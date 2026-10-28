"""The recorder worker: recording, schedules, retention, storage health and motion."""
from __future__ import annotations

from app.backfill import BackfillManager
from app.recording import RecordingManager
from app.workers import runtime


class RecorderService:
    def __init__(self) -> None:
        self.manager = RecordingManager()
        self.backfill = BackfillManager()

    async def start(self) -> None:
        await self.manager.start()
        await self.backfill.start()

    async def stop(self) -> None:
        await self.backfill.stop()
        await self.manager.stop()

    async def handle(self, message: dict[str, object]) -> None:
        if message.get("op") == "sync_camera" and isinstance(message.get("camera_id"), str):
            await self.manager.sync_camera(message["camera_id"], force_restart=bool(message.get("force_restart")))


if __name__ == "__main__":
    runtime.main("recorder", RecorderService())
