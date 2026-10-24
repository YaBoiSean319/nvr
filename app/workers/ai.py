"""The AI worker: person/vehicle analysis on selected cameras."""
from __future__ import annotations

from app.detection import DetectionManager
from app.workers import runtime


class DetectionService:
    def __init__(self) -> None:
        self.manager = DetectionManager()

    async def start(self) -> None:
        await self.manager.start()

    async def stop(self) -> None:
        await self.manager.stop()

    async def handle(self, message: dict[str, object]) -> None:
        if message.get("op") == "reconcile":
            await self.manager.reconcile()
        elif message.get("op") == "sync_camera" and isinstance(message.get("camera_id"), str):
            await self.manager.sync_camera(message["camera_id"], force_restart=bool(message.get("force_restart")))


if __name__ == "__main__":
    runtime.main("ai", DetectionService())
