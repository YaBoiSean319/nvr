"""The background worker: signed webhook delivery, push notifications, rule actions, PTZ tours, footage signing, the nightly self-test and weekly coverage report, the weekly camera hardening checks, the recheck of failed camera capabilities, the external heartbeat, configuration backups, event digests, camera health, building-system events and Home Assistant, and the multi-site console's site health, and the failover recorder's lease, configuration copy and takeover, and the move of older footage to cold storage tiers."""
from __future__ import annotations

from app.backup import BackupManager
from app.camera_health import HealthManager
from app.capabilities import CapabilityManager
from app.console import ConsoleManager
from app.digests import DigestManager
from app.external_events import ExternalEventManager
from app.failover import FailoverManager
from app.hardening import HardeningManager
from app.heartbeat import HeartbeatManager
from app.home_assistant import HomeAssistantManager
from app.push import PushManager
from app.footage_chain import FootageChainManager
from app.ptz import PtzTourManager
from app.rule_actions import RuleActionManager
from app.self_test import SelfTestManager
from app.tiering import TieringManager
from app.webhooks import WebhookManager
from app.workers import runtime


class BackgroundService:
    def __init__(self) -> None:
        self.webhooks = WebhookManager()
        self.push = PushManager()
        self.rule_actions = RuleActionManager()
        self.ptz_tours = PtzTourManager()
        self.footage_chain = FootageChainManager()
        self.self_test = SelfTestManager()
        self.hardening = HardeningManager()
        self.capabilities = CapabilityManager()
        self.heartbeat = HeartbeatManager()
        self.backups = BackupManager()
        self.digests = DigestManager()
        self.camera_health = HealthManager()
        self.external_events = ExternalEventManager()
        self.home_assistant = HomeAssistantManager()
        self.console = ConsoleManager()
        self.failover = FailoverManager()
        self.tiering = TieringManager()

    async def start(self) -> None:
        await self.webhooks.start()
        await self.push.start()
        await self.rule_actions.start()
        await self.ptz_tours.start()
        await self.footage_chain.start()
        await self.self_test.start()
        await self.hardening.start()
        await self.capabilities.start()
        await self.heartbeat.start()
        await self.backups.start()
        await self.digests.start()
        await self.camera_health.start()
        await self.external_events.start()
        await self.home_assistant.start()
        await self.console.start()
        await self.failover.start()
        await self.tiering.start()

    async def stop(self) -> None:
        await self.tiering.stop()
        await self.failover.stop()
        await self.console.stop()
        await self.home_assistant.stop()
        await self.external_events.stop()
        await self.camera_health.stop()
        await self.digests.stop()
        await self.backups.stop()
        await self.heartbeat.stop()
        await self.capabilities.stop()
        await self.hardening.stop()
        await self.self_test.stop()
        await self.footage_chain.stop()
        await self.ptz_tours.stop()
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
