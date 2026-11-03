"""AI analysis without a fixed camera cap (#238): the number of cameras analysed follows what the hardware can handle, and the page can say what it is doing.

``ai_runtime_settings`` is rebuilt to drop the limit of 16 cameras (SQLite cannot change a CHECK in place) and gains ``capacity_mode`` ('auto', the default, or 'fixed' with the
number in ``max_active_cameras``). ``ai_capacity_status`` is what the AI worker last measured and decided, for the settings page; its ``limit_cameras`` is also where the worker
resumes after a restart.
"""
from __future__ import annotations

import sqlite3

NAME = "ai_capacity"

_STATEMENTS = (
    """
    CREATE TABLE ai_runtime_settings_new (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
        max_active_cameras INTEGER NOT NULL DEFAULT 4 CHECK (max_active_cameras >= 1),
        capacity_mode TEXT NOT NULL DEFAULT 'auto' CHECK (capacity_mode IN ('auto','fixed')),
        provider TEXT NOT NULL DEFAULT 'CPU' CHECK (provider IN ('CPU','GPU')),
        model_id TEXT NOT NULL DEFAULT 'person-vehicle-bike-detection-2004',
        model_version TEXT NOT NULL DEFAULT '2021.4-FP32',
        validated_at TEXT,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    INSERT INTO ai_runtime_settings_new(singleton_id,max_active_cameras,capacity_mode,provider,model_id,model_version,validated_at,updated_at)
    SELECT singleton_id,max_active_cameras,'auto',provider,model_id,model_version,validated_at,updated_at FROM ai_runtime_settings
    """,
    "DROP TABLE ai_runtime_settings",
    "ALTER TABLE ai_runtime_settings_new RENAME TO ai_runtime_settings",
)

_STATUS = """
CREATE TABLE IF NOT EXISTS ai_capacity_status (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
    mode TEXT NOT NULL DEFAULT 'auto',
    limit_cameras INTEGER NOT NULL DEFAULT 4,
    analysed INTEGER NOT NULL DEFAULT 0,
    wanting INTEGER NOT NULL DEFAULT 0,
    factor TEXT,
    action TEXT,
    cpu REAL,
    cores REAL,
    inference_busy REAL,
    memory_used_bytes INTEGER,
    memory_total_bytes INTEGER,
    late_share REAL,
    detail TEXT,
    updated_at TEXT NOT NULL
)
"""


def upgrade(db: sqlite3.Connection) -> None:
    if "capacity_mode" not in {row[1] for row in db.execute("PRAGMA table_info(ai_runtime_settings)")}:
        for statement in _STATEMENTS:
            db.execute(statement)
    db.execute(_STATUS)


def verify(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(ai_runtime_settings)")}
    if "capacity_mode" not in columns:
        raise RuntimeError("ai_runtime_settings has no capacity_mode")
    if db.execute("SELECT 1 FROM ai_runtime_settings WHERE singleton_id=1").fetchone() is None:
        raise RuntimeError("ai_runtime_settings lost its row")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_capacity_status'").fetchone() is None:
        raise RuntimeError("ai_capacity_status was not created")
