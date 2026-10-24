"""The rules worker: evaluates rules over new detections and records rule events."""
from __future__ import annotations

from app.rules import RulesManager
from app.workers import runtime


class RulesService:
    def __init__(self) -> None:
        self.manager = RulesManager()

    async def start(self) -> None:
        await self.manager.start()

    async def stop(self) -> None:
        await self.manager.stop()

    async def handle(self, message: dict[str, object]) -> None:
        if message.get("op") == "evaluate":
            self.manager.nudge()


if __name__ == "__main__":
    runtime.main("rules", RulesService())
