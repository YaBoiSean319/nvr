"""Camera health (#137): what each camera says about itself and what the NVR has measured, kept so the fleet view starts no media.

The camera's own report (make, model, firmware, its configured bitrate and frame rate) comes from ONVIF, with the time of the last successful contact.
The measured values come from the recordings; the recorder's counters (reconnects, dropped frames) are in ``metric_counters`` with the other
recorder counters, and ``stream_up_since`` is when the recorder last began recording after a break.
"""
from __future__ import annotations

import sqlite3

NAME = "camera_health"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE camera_health (
            camera_id TEXT PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
            manufacturer TEXT,
            model TEXT,
            firmware TEXT,
            configured_codec TEXT,
            configured_width INTEGER,
            configured_height INTEGER,
            configured_fps REAL,
            configured_bitrate_kbps INTEGER,
            onvif_contact_at TEXT,
            onvif_checked_at TEXT,
            onvif_error TEXT,
            measured_fps REAL,
            measured_bitrate_kbps REAL,
            measured_at TEXT,
            stream_up_since TEXT
        )
        """
    )
    # Every writer of the recorder's state counts here, however it writes it: a trigger cannot be forgotten by a code path.
    db.execute(
        """
        CREATE TRIGGER camera_health_recorder_state AFTER UPDATE OF recording_status ON cameras
        WHEN OLD.recording_status IS NOT NEW.recording_status
        BEGIN
            INSERT OR IGNORE INTO camera_health(camera_id) VALUES (NEW.id);
            UPDATE camera_health SET stream_up_since=CASE
                WHEN NEW.recording_status IN ('recording','buffering')
                    THEN COALESCE(CASE WHEN OLD.recording_status IN ('recording','buffering') THEN stream_up_since END, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ELSE NULL END
            WHERE camera_id=NEW.id;
            INSERT OR IGNORE INTO metric_counters(name,camera_id,value) SELECT 'reconnects',NEW.id,0 WHERE NEW.recording_status='reconnecting';
            UPDATE metric_counters SET value=value+1 WHERE name='reconnects' AND camera_id=NEW.id AND NEW.recording_status='reconnecting';
        END
        """
    )
