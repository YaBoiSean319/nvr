"""Motion events: the indexes their readers need, and removal with their footage (#333).

``motion_events.segment_id`` refers to a recorded segment with ON DELETE SET NULL, and nothing indexed it. Every time a segment was deleted, SQLite
looked for the motion events pointing at it by reading the whole table, inside the delete's write transaction: 0.3 ms with an empty table, 442 ms
with two million rows, per deleted segment. Nothing ever removed a motion event, so the table only grew.

- An index on ``segment_id`` makes that lookup a seek.
- An index on ``(camera_id, source_event_at)`` serves the Playback timeline, which filtered and sorted a camera's events by that time without one.
- A trigger removes a clip's motion event together with its segment: an event whose footage is gone points at nothing. (It runs before the
  delete, so it sees the event while it still names the segment.)
- Events whose footage was already removed are cleared once, here. Events that never produced footage (failed ones) are kept for the camera's
  retention period by the retention pass, since they explain a gap.
"""
from __future__ import annotations

import sqlite3

NAME = "motion_event_indexes"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS motion_events_segment_idx ON motion_events(segment_id)")
    db.execute("CREATE INDEX IF NOT EXISTS motion_events_camera_event_idx ON motion_events(camera_id, source_event_at)")
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS recording_segments_remove_motion_events BEFORE DELETE ON recording_segments "
        "BEGIN DELETE FROM motion_events WHERE segment_id=OLD.id; END"
    )
    db.execute("DELETE FROM motion_events WHERE status='complete' AND segment_id IS NULL")
