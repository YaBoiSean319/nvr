"""Detections and tracks as structured data.

Each detection is one classified box at one sampled time; a track links one
object's detections. Track summaries are kept by triggers so every writer and
every deletion path (retention, footage deletion, camera removal) keeps them true.
Deleting footage deletes the detections it covers, unless other footage still
covers the same moment.
"""
from __future__ import annotations

import sqlite3

NAME = "detections"


_STATEMENTS = (
    """
    CREATE TABLE ai_tracks (
        id TEXT PRIMARY KEY,
        camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        label TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        detection_count INTEGER NOT NULL DEFAULT 0,
        max_confidence REAL NOT NULL DEFAULT 0,
        model_id TEXT NOT NULL,
        model_version TEXT NOT NULL,
        provider TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX ai_tracks_camera_time_idx ON ai_tracks(camera_id,first_seen_at)
    """,
    """
    CREATE INDEX ai_tracks_camera_last_seen_idx ON ai_tracks(camera_id,last_seen_at)
    """,
    """
    CREATE TABLE ai_detections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
        track_id TEXT NOT NULL REFERENCES ai_tracks(id) ON DELETE CASCADE,
        detected_at TEXT NOT NULL,
        label TEXT NOT NULL,
        confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
        box_x REAL NOT NULL CHECK (box_x BETWEEN 0 AND 1),
        box_y REAL NOT NULL CHECK (box_y BETWEEN 0 AND 1),
        box_width REAL NOT NULL CHECK (box_width > 0 AND box_width <= 1),
        box_height REAL NOT NULL CHECK (box_height > 0 AND box_height <= 1),
        attributes_json TEXT NOT NULL DEFAULT '{}',
        model_id TEXT NOT NULL,
        model_version TEXT NOT NULL,
        provider TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX ai_detections_camera_time_idx ON ai_detections(camera_id,detected_at)
    """,
    """
    CREATE INDEX ai_detections_track_time_idx ON ai_detections(track_id,detected_at)
    """,
    """
    CREATE TRIGGER ai_detections_track_added AFTER INSERT ON ai_detections BEGIN
        UPDATE ai_tracks SET
            first_seen_at=CASE WHEN detection_count=0 THEN NEW.detected_at ELSE min(first_seen_at,NEW.detected_at) END,
            last_seen_at=CASE WHEN detection_count=0 THEN NEW.detected_at ELSE max(last_seen_at,NEW.detected_at) END,
            max_confidence=max(max_confidence,NEW.confidence),
            detection_count=detection_count+1
        WHERE id=NEW.track_id;
    END
    """,
    """
    CREATE TRIGGER ai_detections_track_removed AFTER DELETE ON ai_detections BEGIN
        DELETE FROM ai_tracks WHERE id=OLD.track_id
            AND NOT EXISTS (SELECT 1 FROM ai_detections WHERE track_id=OLD.track_id);
        UPDATE ai_tracks SET
            first_seen_at=(SELECT min(detected_at) FROM ai_detections WHERE track_id=OLD.track_id),
            last_seen_at=(SELECT max(detected_at) FROM ai_detections WHERE track_id=OLD.track_id),
            max_confidence=(SELECT max(confidence) FROM ai_detections WHERE track_id=OLD.track_id),
            detection_count=detection_count-1
        WHERE id=OLD.track_id;
    END
    """,
    """
    CREATE TRIGGER recording_segments_remove_detections AFTER DELETE ON recording_segments BEGIN
        DELETE FROM ai_detections
        WHERE camera_id=OLD.camera_id AND detected_at>=OLD.starts_at AND detected_at<OLD.ends_at
          AND NOT EXISTS (
              SELECT 1 FROM recording_segments s
              WHERE s.camera_id=OLD.camera_id AND s.starts_at<=ai_detections.detected_at AND s.ends_at>ai_detections.detected_at
          );
    END
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)


def verify(db: sqlite3.Connection) -> None:
    triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    missing = {"ai_detections_track_added", "ai_detections_track_removed", "recording_segments_remove_detections"} - triggers
    if missing:
        raise RuntimeError(f"Detection triggers are missing: {sorted(missing)}")
