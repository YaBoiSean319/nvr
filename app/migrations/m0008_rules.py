"""Rules over detections and tracks, and the rule events they produce.

A rule event is unique per rule and track: one event for each object that meets a
rule, however many frames it meets it in. Events outlive a deleted rule (they keep
its name) so alarm and audit history stays complete. ``rules_cursor`` records the last
detection the rules worker has evaluated.
"""
from __future__ import annotations

import sqlite3

NAME = "rules"

_STATEMENTS = (
    """
    CREATE TABLE rules (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
        camera_ids_json TEXT NOT NULL DEFAULT '[]',
        group_ids_json TEXT NOT NULL DEFAULT '[]',
        condition_type TEXT NOT NULL CHECK (condition_type IN ('zone_entry','line_crossing','loitering','wrong_way')),
        condition_json TEXT NOT NULL,
        classes_json TEXT NOT NULL,
        schedule_json TEXT,
        cooldown_seconds INTEGER NOT NULL DEFAULT 0 CHECK (cooldown_seconds BETWEEN 0 AND 86400),
        active_from TEXT NOT NULL,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE rule_events (
        id TEXT PRIMARY KEY,
        rule_id TEXT REFERENCES rules(id) ON DELETE SET NULL,
        rule_name TEXT NOT NULL,
        camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        track_id TEXT NOT NULL,
        detection_id INTEGER,
        label TEXT NOT NULL,
        condition_type TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        box_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        UNIQUE (rule_id, track_id)
    )
    """,
    """
    CREATE INDEX rule_events_time_idx ON rule_events(occurred_at)
    """,
    """
    CREATE INDEX rule_events_rule_camera_time_idx ON rule_events(rule_id,camera_id,occurred_at)
    """,
    """
    CREATE TABLE rules_cursor (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
        last_detection_id INTEGER NOT NULL
    )
    """,
    # Rules start with detections stored after they exist, not the history before.
    """
    INSERT INTO rules_cursor(singleton_id,last_detection_id) SELECT 1,coalesce(max(id),0) FROM ai_detections
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
