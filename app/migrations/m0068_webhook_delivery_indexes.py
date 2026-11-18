"""Webhook deliveries: indexes for counting what is pending and for the newest-first list (#350).

Each new event counts a receiver's pending deliveries, and for AI detections that happens inside the AI worker's per-frame write transaction.
The only index that helped put the receiver first and the time second, so the count read the receiver's whole history: 103 ms an event with
500,000 delivered rows. The deliveries list sorted the whole table. Delivery history is now also pruned (see ``app.webhooks.prune_deliveries``).
"""
from __future__ import annotations

import sqlite3

NAME = "webhook_delivery_indexes"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS webhook_deliveries_endpoint_status_idx ON webhook_deliveries(endpoint_id, status)")
    db.execute("CREATE INDEX IF NOT EXISTS webhook_deliveries_created_idx ON webhook_deliveries(created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS webhook_deliveries_finished_idx ON webhook_deliveries(status, updated_at)")
