"""Alarms: rule events with a handling state, comments and a history.

Alarms are distinct from incidents, which stay system faults. Each alarm keeps the
rule, camera and object it was raised for even after the rule or its event is
deleted. ``alarm_settings`` holds how long resolved alarms keep their footage.
"""
from __future__ import annotations

import sqlite3

NAME = "alarms"

_STATEMENTS = (
    """
    CREATE TABLE alarms (
        id TEXT PRIMARY KEY,
        rule_event_id TEXT UNIQUE REFERENCES rule_events(id) ON DELETE SET NULL,
        rule_id TEXT REFERENCES rules(id) ON DELETE SET NULL,
        rule_name TEXT NOT NULL,
        camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        track_id TEXT,
        label TEXT NOT NULL,
        condition_type TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        box_json TEXT NOT NULL,
        clip_starts_at TEXT NOT NULL,
        clip_ends_at TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'new' CHECK (state IN ('new','acknowledged','assigned','resolved')),
        assigned_to INTEGER REFERENCES users(id) ON DELETE SET NULL,
        acknowledged_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        acknowledged_at TEXT,
        resolved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        resolved_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX alarms_state_time_idx ON alarms(state,occurred_at DESC)
    """,
    """
    CREATE INDEX alarms_camera_clip_idx ON alarms(camera_id,clip_starts_at,clip_ends_at)
    """,
    """
    CREATE TABLE alarm_comments (
        id TEXT PRIMARY KEY,
        alarm_id TEXT NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
        author_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE alarm_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alarm_id TEXT NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
        actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        action TEXT NOT NULL,
        details_json TEXT NOT NULL DEFAULT '{}',
        occurred_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX alarm_history_alarm_idx ON alarm_history(alarm_id,id)
    """,
    """
    CREATE TABLE alarm_settings (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
        resolved_footage_days INTEGER NOT NULL DEFAULT 30 CHECK (resolved_footage_days BETWEEN 0 AND 3650),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    INSERT INTO alarm_settings(singleton_id) VALUES(1)
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
