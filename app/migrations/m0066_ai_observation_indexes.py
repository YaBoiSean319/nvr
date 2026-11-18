"""AI observations: the indexes their readers need (#345).

For every object in every analysed frame, the AI worker looks for a recent observation of the same thing to extend. That lookup filters and
sorts on ``last_seen_at``, which nothing indexed, so it read every observation ever stored for the camera and label, inside the frame's write
transaction: 6 ms a frame with 10,000 rows, 732 ms with a million. The Playback timeline and the AI events list read a camera's observations by
``first_seen_at`` without an index that fits (the existing one puts the label first).

- ``(camera_id, label, last_seen_at)`` serves the per-frame lookup: a seek to the last few seconds, already in order.
- ``(camera_id, first_seen_at)`` serves the timeline, and the retention pass that now removes observations older than the camera's policy.
"""
from __future__ import annotations

import sqlite3

NAME = "ai_observation_indexes"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS ai_observations_recent_idx ON ai_observations(camera_id, label, last_seen_at)")
    db.execute("CREATE INDEX IF NOT EXISTS ai_observations_camera_time_idx ON ai_observations(camera_id, first_seen_at)")
