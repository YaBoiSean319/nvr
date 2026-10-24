"""The export worker: renders queued export jobs into evidence packages."""
from __future__ import annotations

from app.exports import ExportManager
from app.workers import runtime


class ExportService:
    def __init__(self) -> None:
        self.manager = ExportManager()

    async def start(self) -> None:
        await self.manager.start()

    async def stop(self) -> None:
        await self.manager.stop()

    async def handle(self, message: dict[str, object]) -> None:
        job_id = message.get("job_id")
        if not isinstance(job_id, str):
            return
        if message.get("op") == "enqueue":
            self.manager.enqueue(job_id)
        elif message.get("op") == "cancel":
            await self.manager.cancel(job_id)


if __name__ == "__main__":
    runtime.main("exporter", ExportService())
