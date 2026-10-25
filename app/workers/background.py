"""The background worker: signed webhook delivery, push notifications, rule actions, the external heartbeat and configuration backups."""
from __future__ import annotations

from app.backup import BackupManager
from app.heartbeat import HeartbeatManager
from app.push import PushManager
from app.rule_actions import RuleActionManager
from app.webhooks import WebhookManager
from app.workers import runtime


class BackgroundService:
    def __init__(self) -> None:
        self.webhooks = WebhookManager()
        self.push = PushManager()
        self.rule_actions = RuleActionManager()
        self.heartbeat = HeartbeatManager()
        self.backups = BackupManager()

    async def start(self) -> None:
        await self.webhooks.start()
        await self.push.start()
        await self.rule_actions.start()
        await self.heartbeat.start()
        await self.backups.start()

    async def stop(self) -> None:
        await self.backups.stop()
        await self.heartbeat.stop()
        await self.rule_actions.stop()
        await self.push.stop()
        await self.webhooks.stop()

    async def handle(self, message: dict[str, object]) -> None:
        # Webhook and heartbeat work is queued in the database and picked up each
        # second; a requested backup is started now rather than at the next poll.
        if message.get("op") == "backup":
            self.backups.nudge()


if __name__ == "__main__":
    runtime.main("background", BackgroundService())
