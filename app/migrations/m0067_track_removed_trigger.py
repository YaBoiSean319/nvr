"""Removing a detection no longer re-reads the whole of its track (#346).

When a detection is deleted (its footage expired), a trigger keeps its track's summary right. It recomputed the track's best confidence with
``max(confidence)`` over every remaining detection of the track, once per deleted row: no index can answer that, so clearing a long track (a
parked car, someone at a desk) cost the square of its length. With twenty objects in view for a day, deleting one expired minute of footage
held the write lock for 2.4 s.

The first and last times are still recomputed: the index on (track, time) answers each with one seek. The best confidence is no longer
recomputed. It becomes the best the object was ever seen with, which is what "how sure was the detector that this was a person" means for a
track, and it only differed from the old value when the single best sighting happened to be in footage that had expired while the rest was kept.
"""
from __future__ import annotations

import sqlite3

NAME = "track_removed_trigger"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("DROP TRIGGER IF EXISTS ai_detections_track_removed")
    db.execute(
        """
        CREATE TRIGGER ai_detections_track_removed AFTER DELETE ON ai_detections BEGIN
            DELETE FROM ai_tracks WHERE id=OLD.track_id
                AND NOT EXISTS (SELECT 1 FROM ai_detections WHERE track_id=OLD.track_id);
            UPDATE ai_tracks SET
                first_seen_at=(SELECT min(detected_at) FROM ai_detections WHERE track_id=OLD.track_id),
                last_seen_at=(SELECT max(detected_at) FROM ai_detections WHERE track_id=OLD.track_id),
                detection_count=detection_count-1
            WHERE id=OLD.track_id;
        END
        """
    )
