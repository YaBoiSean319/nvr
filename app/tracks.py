"""Detections as structured data, linked into tracks.

A detection is one classified box at one sampled time. ``Tracker`` links a
camera's detections into tracks by box overlap (IoU) and appearance (a colour
histogram of the box), so an object keeps one track id while it moves and two
objects that cross keep theirs. Tracker state lives in memory for one camera's
analysis loop; after a restart, objects start new tracks.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import numpy as np

from .attributes import TrackProfile
from .detection import MODEL_HEIGHT, MODEL_WIDTH, box_iou

HISTOGRAM_BINS = 8


@dataclass
class _ActiveTrack:
    id: str
    label: str
    box: dict[str, float]
    seen_at: datetime
    appearance: np.ndarray | None


def appearance(frame: bytes | None, box: dict[str, float]) -> np.ndarray | None:
    """A normalized per-channel colour histogram of the box inside a detector-sized BGR frame."""
    if frame is None or len(frame) != MODEL_WIDTH * MODEL_HEIGHT * 3:
        return None
    image = np.frombuffer(frame, dtype=np.uint8).reshape(MODEL_HEIGHT, MODEL_WIDTH, 3)
    x1 = int(box["x"] * MODEL_WIDTH)
    y1 = int(box["y"] * MODEL_HEIGHT)
    x2 = max(x1 + 1, int(round((box["x"] + box["width"]) * MODEL_WIDTH)))
    y2 = max(y1 + 1, int(round((box["y"] + box["height"]) * MODEL_HEIGHT)))
    crop = image[y1:y2, x1:x2].reshape(-1, 3)
    if crop.size == 0:
        return None
    histogram = np.concatenate([
        np.bincount(crop[:, channel] // (256 // HISTOGRAM_BINS), minlength=HISTOGRAM_BINS) for channel in range(3)
    ]).astype(np.float32)
    return histogram / histogram.sum()


def _similarity(left: np.ndarray | None, right: np.ndarray | None) -> float:
    if left is None or right is None:
        return 0.0
    # Histogram intersection: 1.0 for identical colour distributions.
    return float(np.minimum(left, right).sum())


def _center_distance(left: dict[str, float], right: dict[str, float]) -> float:
    dx = (left["x"] + left["width"] / 2) - (right["x"] + right["width"] / 2)
    dy = (left["y"] + left["height"] / 2) - (right["y"] + right["height"] / 2)
    return float((dx * dx + dy * dy) ** 0.5)


class Tracker:
    """Links one camera's detections into tracks, one frame at a time."""

    def __init__(self, max_gap: timedelta = timedelta(seconds=10), min_iou: float = 0.1,
                 min_similarity: float = 0.6, max_jump: float = 0.2):
        self.max_gap = max_gap
        self.min_iou = min_iou
        self.min_similarity = min_similarity
        self.max_jump = max_jump
        self.active: list[_ActiveTrack] = []
        self.profiles: dict[str, TrackProfile] = {}

    def profile(self, track_id: str) -> TrackProfile:
        """What a live track has shown so far (its attribute votes, direction and places passed)."""
        return self.profiles.setdefault(track_id, TrackProfile())

    def assign(self, detections: list[dict[str, Any]], at: datetime, frame: bytes | None = None) -> list[tuple[str, bool]]:
        """Return ``(track_id, is_new)`` for each detection, in order."""
        self.active = [track for track in self.active if at - track.seen_at <= self.max_gap]
        live = {track.id for track in self.active}
        self.profiles = {track_id: profile for track_id, profile in self.profiles.items() if track_id in live}
        looks = [appearance(frame, item["box"]) for item in detections]
        candidates: list[tuple[float, int, int]] = []
        for d_index, item in enumerate(detections):
            for t_index, track in enumerate(self.active):
                if track.label != item["label"]:
                    continue
                overlap = box_iou(track.box, item["box"])
                similarity = _similarity(track.appearance, looks[d_index])
                if track.appearance is not None and looks[d_index] is not None and similarity < self.min_similarity:
                    continue  # boxes may touch, but this is a different-looking object
                if overlap >= self.min_iou or (similarity >= self.min_similarity and _center_distance(track.box, item["box"]) <= self.max_jump):
                    candidates.append((overlap + similarity, d_index, t_index))
        matched: dict[int, int] = {}
        used: set[int] = set()
        for _, d_index, t_index in sorted(candidates, reverse=True):
            if d_index in matched or t_index in used:
                continue
            matched[d_index] = t_index
            used.add(t_index)
        result: list[tuple[str, bool]] = []
        for d_index, item in enumerate(detections):
            if d_index in matched:
                track = self.active[matched[d_index]]
                track.box, track.seen_at = item["box"], at
                if looks[d_index] is not None:
                    track.appearance = looks[d_index] if track.appearance is None else (track.appearance + looks[d_index]) / 2
                result.append((track.id, False))
            else:
                track = _ActiveTrack(str(uuid.uuid4()), str(item["label"]), item["box"], at, looks[d_index])
                self.active.append(track)
                result.append((track.id, True))
        return result


def record_detections(
    db: sqlite3.Connection,
    camera_id: str,
    detections: list[dict[str, Any]],
    at: datetime,
    tracker: Tracker,
    *,
    frame: bytes | None = None,
    model_id: str,
    model_version: str,
    provider: str,
) -> list[str]:
    """Store one frame's detections and return the track id of each."""
    stamp = at.astimezone(timezone.utc).isoformat()
    track_ids: list[str] = []
    for item, (track_id, _) in zip(detections, tracker.assign(detections, at, frame), strict=True):
        # Also recreates a track whose earlier detections were deleted with their footage mid-track.
        db.execute(
            "INSERT OR IGNORE INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,?,?,?,?,?,?)",
            (track_id, camera_id, item["label"], stamp, stamp, model_id, model_version, provider),
        )
        box = item["box"]
        db.execute(
            "INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,attributes_json,model_id,model_version,provider) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (camera_id, track_id, stamp, item["label"], min(1.0, max(0.0, float(item["confidence"]))), box["x"], box["y"], box["width"], box["height"],
             json.dumps(item.get("attributes") or {}, separators=(",", ":"), sort_keys=True), model_id, model_version, provider),
        )
        track_ids.append(track_id)
        tracker.profile(track_id).add(item.get("attributes") or {}, box)
    for track_id in dict.fromkeys(track_ids):
        _store_attributes(db, track_id, tracker.profile(track_id))
    return track_ids


def _store_attributes(db: sqlite3.Connection, track_id: str, profile: TrackProfile) -> None:
    """Write the track's attributes when they have changed since they were last written."""
    summary = profile.summary()
    if summary == profile.stored:
        return
    # A value this profile has not seen never replaces one already stored (the profile starts empty when the worker restarts and picks up a track again),
    # and the cells passed through only ever accumulate.
    db.execute("UPDATE ai_tracks SET upper_colour=COALESCE(?,upper_colour),lower_colour=COALESCE(?,lower_colour),vehicle_colour=COALESCE(?,vehicle_colour),"
               "vehicle_type=COALESCE(?,vehicle_type),direction=COALESCE(?,direction),zone_mask=zone_mask|? WHERE id=?",
               (summary["upper_colour"], summary["lower_colour"], summary["vehicle_colour"], summary["vehicle_type"], summary["direction"], summary["zone_mask"], track_id))
    profile.stored = summary


def purge_expired_detections(db: sqlite3.Connection, camera_id: str, before: datetime) -> int:
    """Delete detections older than retention that no remaining footage covers."""
    limit = before.astimezone(timezone.utc).isoformat()
    # Plate reads follow their footage as detections do.
    db.execute(
        "DELETE FROM plate_reads WHERE camera_id=? AND read_at<? AND NOT EXISTS ("
        "SELECT 1 FROM recording_segments s WHERE s.camera_id=plate_reads.camera_id "
        "AND s.starts_at<=plate_reads.read_at AND s.ends_at>plate_reads.read_at)", (camera_id, limit))
    return db.execute(
        "DELETE FROM ai_detections WHERE camera_id=? AND detected_at<? AND NOT EXISTS ("
        "SELECT 1 FROM recording_segments s WHERE s.camera_id=ai_detections.camera_id "
        "AND s.starts_at<=ai_detections.detected_at AND s.ends_at>ai_detections.detected_at)",
        (camera_id, limit),
    ).rowcount


def detection_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "camera_id": row["camera_id"], "track_id": row["track_id"], "detected_at": row["detected_at"],
        "label": row["label"], "confidence": row["confidence"],
        "box": {"x": row["box_x"], "y": row["box_y"], "width": row["box_width"], "height": row["box_height"]},
        "attributes": json.loads(row["attributes_json"]),
        "model_id": row["model_id"], "model_version": row["model_version"], "provider": row["provider"],
    }


def track_view(row: sqlite3.Row) -> dict[str, Any]:
    view = {key: row[key] for key in ("id", "camera_id", "label", "first_seen_at", "last_seen_at", "detection_count",
                                      "max_confidence", "model_id", "model_version", "provider")}
    view["attributes"] = {key: row[key] for key in ("upper_colour", "lower_colour", "vehicle_colour", "vehicle_type", "direction") if row[key]}
    return view


def iter_unanalyzed(db: sqlite3.Connection, camera_id: str, start: str, end: str) -> Iterable[dict[str, Any]]:
    return (dict(row) for row in db.execute(
        "SELECT state,starts_at,ends_at,detail FROM ai_coverage "
        "WHERE camera_id=? AND state!='analyzed' AND starts_at<? AND (ends_at IS NULL OR ends_at>?) ORDER BY starts_at LIMIT 500",
        (camera_id, end, start)))


def encode_jpeg(frame: bytes) -> bytes | None:
    """A detector-sized BGR frame as a JPEG, or None when it cannot be encoded."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or len(frame) != MODEL_WIDTH * MODEL_HEIGHT * 3:
        return None
    try:
        result = subprocess.run([ffmpeg, "-v", "error", "-threads", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{MODEL_WIDTH}x{MODEL_HEIGHT}",
                                 "-i", "pipe:0", "-frames:v", "1", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                                input=frame, capture_output=True, timeout=5, check=False)
    except subprocess.TimeoutExpired:
        return None
    return result.stdout if result.returncode == 0 and result.stdout.startswith(b"\xff\xd8") else None


def store_keyframe(db: sqlite3.Connection, track_id: str, at: datetime, jpeg: bytes) -> None:
    db.execute("INSERT OR IGNORE INTO track_keyframes(track_id,captured_at,jpeg) VALUES(?,?,?)", (track_id, at.astimezone(timezone.utc).isoformat(), jpeg))
