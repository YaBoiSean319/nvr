"""The background worker: signed webhook delivery and the external heartbeat."""
from __future__ import annotations

from app.heartbeat import HeartbeatManager
from app.webhooks import WebhookManager
from app.workers import runtime


class BackgroundService:
    def __init__(self) -> None:
        self.webhooks = WebhookManager()
        self.heartbeat = HeartbeatManager()

    async def start(self) -> None:
        await self.webhooks.start()
        await self.heartbeat.start()

    async def stop(self) -> None:
        await self.heartbeat.stop()
        await self.webhooks.stop()

    async def handle(self, message: dict[str, object]) -> None:
        # Webhook and heartbeat work is queued in the database and picked up each second.
        return None


if __name__ == "__main__":
    runtime.main("background", BackgroundService())
