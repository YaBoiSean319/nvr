"""Alarms and rule events: how long their history is kept, and indexes that serve their lists in order (#349).

``alarm_settings.history_days`` (90 by default, the owner's choice) is how long resolved alarms and rule events are kept. The indexes let the
alarm list, which every open browser tab asks for every five seconds, and the rule-event list read their newest rows in order instead of sorting
the whole table: with 500,000 open alarms one poll took 3.3 s.
"""
from __future__ import annotations

import sqlite3

NAME = "alarm_history"

_STATEMENTS = (
    "ALTER TABLE alarm_settings ADD COLUMN history_days INTEGER NOT NULL DEFAULT 90 CHECK (history_days BETWEEN 7 AND 3650)",
    # The open alarms, newest first: the list's default.
    "CREATE INDEX IF NOT EXISTS alarms_open_order_idx ON alarms(occurred_at DESC, id) WHERE state IN ('new','acknowledged','assigned')",
    "CREATE INDEX IF NOT EXISTS alarms_state_order_idx ON alarms(state, occurred_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS alarms_order_idx ON alarms(occurred_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS alarms_camera_order_idx ON alarms(camera_id, occurred_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS alarms_rule_order_idx ON alarms(rule_id, occurred_at DESC, id)",
    # The rule filter: every rule that has raised an alarm.
    "CREATE INDEX IF NOT EXISTS alarms_rule_name_idx ON alarms(rule_id, rule_name)",
    # What the history pass removes: alarms resolved long ago, and open ones nobody has handled.
    "CREATE INDEX IF NOT EXISTS alarms_resolved_idx ON alarms(resolved_at) WHERE resolved_at IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS rule_events_order_idx ON rule_events(occurred_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS rule_events_camera_order_idx ON rule_events(camera_id, occurred_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS rule_events_rule_order_idx ON rule_events(rule_id, occurred_at DESC, id)",
    # The superseded ones: each is a prefix of, or answered by, one above.
    "DROP INDEX IF EXISTS alarms_state_time_idx",
)


def upgrade(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(alarm_settings)")}
    for statement in _STATEMENTS:
        if statement.startswith("ALTER") and "history_days" in columns:
            continue
        db.execute(statement)
