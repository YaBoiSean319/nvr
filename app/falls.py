"""Fall detection (#230): an alert when a person falls and stays down.

The AI worker already finds and follows each person (see ``app.tracks``). For a camera that has fall detection on, it asks a pose model (``app.pose_models``) for the
position of the person's shoulders, hips, knees and ankles about twice a second, and this module decides from those points whether the person has fallen. The decision
works on those few body points only, so a different (permissively licensed) pose model can be used without changing what a fall is.

A fall is: a person who was upright, then low (the body near horizontal) within a few seconds, who then stays low for the stay-down time. That is judged for each track:

* Someone who is **first seen already lying** (a resident in bed, on a sofa) is not a fall: nothing is raised without having seen them upright first.
* Someone who **lowers themselves slowly** (more than the transition time between upright and low) is not a fall.
* Someone who **gets up** before the stay-down time has passed is not a fall; a stumble that recovers raises nothing.
* A person who goes **out of view** (hidden behind furniture, a pose that cannot be made out) while low keeps counting as low for a short while, because falling behind a bed is
  what a fall often looks like.
* One fall raises one event; the person must be upright again for a moment before another can.

This is an aid. It will miss falls and it will raise alerts that are not falls, and the sensitivity setting trades one for the other (the default leans to catching falls). It
does not replace call buttons, checks or supervision.
"""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from .store import connect

# The body points the decision uses, by their names, with (x, y, visibility) in the picture's own 0..1 coordinates.
Pose = Mapping[str, tuple[float, float, float]]

SENSITIVITIES = ("low", "normal", "high")
DEFAULT_SENSITIVITY = "high"  # A missed fall costs more than a false alert
DEFAULT_STAY_DOWN_SECONDS = 3.0
MIN_STAY_DOWN_SECONDS, MAX_STAY_DOWN_SECONDS = 1.0, 60.0
SETTINGS_REFRESH = 30.0
MAX_PEOPLE_PER_FRAME = 8  # The most people posed in one analysed frame of one camera: a crowd is not a place this works well, and the cost is a few milliseconds each
FULL_FRAME_BOX = {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
VISIBLE = 0.3  # The least visibility at which a body point is believed

# Per sensitivity: (torso angle from upright above which the person is "low", torso angle below which they are "upright", seconds a drop may take, seconds unseen that still count)
_THRESHOLDS = {"low": (70.0, 30.0, 2.0, 3.0), "normal": (60.0, 35.0, 3.0, 4.0), "high": (50.0, 40.0, 4.0, 6.0)}
REARM_UPRIGHT_SECONDS = 2.0


def _middle(pose: Pose, names: tuple[str, str]) -> tuple[float, float] | None:
    points = [pose[name] for name in names if name in pose and pose[name][2] >= VISIBLE]
    if not points:
        return None
    return sum(point[0] for point in points) / len(points), sum(point[1] for point in points) / len(points)


def torso_angle(pose: Pose, aspect: float = 1.0) -> float | None:
    """How far the torso (shoulders to hips) leans from upright, in degrees: 0 standing straight, 90 lying flat. None when the shoulders or hips cannot be made out.

    ``aspect`` is the picture's width over its height, so an angle measured in its 0..1 coordinates is the angle on the screen."""
    shoulders, hips = _middle(pose, ("left_shoulder", "right_shoulder")), _middle(pose, ("left_hip", "right_hip"))
    if shoulders is None or hips is None:
        return None
    dx, dy = (hips[0] - shoulders[0]) * aspect, hips[1] - shoulders[1]
    if math.hypot(dx, dy) < 1e-6:
        return None
    return math.degrees(math.atan2(abs(dx), abs(dy)))


def body_shape(pose: Pose, aspect: float = 1.0) -> float | None:
    """The body's width over its height from the points that can be made out (more than 1: wider than tall, as a person lying down is)."""
    points = [point for point in pose.values() if point[2] >= VISIBLE]
    if len(points) < 4:
        return None
    xs, ys = [point[0] * aspect for point in points], [point[1] for point in points]
    height = max(max(ys) - min(ys), 0.02)  # A body lying flat has almost no height: wide, not undefined
    return (max(xs) - min(xs)) / height


@dataclass
class _Track:
    upright_at: float | None = None  # Last time the person was seen upright
    low_since: float | None = None  # When they went low, if they are low now and it looks like a fall
    low_ignored: bool = False  # Low, but first seen so or got there too slowly: not a fall
    seen_at: float = 0.0
    fired: bool = False
    upright_since: float | None = None


@dataclass(frozen=True)
class Fall:
    track_id: str
    at: float
    down_seconds: float


@dataclass
class FallDetector:
    """The decision for every person of one camera. ``update`` is called with each analysed frame's pose for a track, in time order (seconds, any clock)."""
    sensitivity: str = DEFAULT_SENSITIVITY
    stay_down_seconds: float = DEFAULT_STAY_DOWN_SECONDS
    aspect: float = 16 / 9
    tracks: dict[str, _Track] = field(default_factory=dict)

    def _classify(self, pose: Pose | None) -> str:
        """"upright", "low" or "unknown" for one observation."""
        if pose is None:
            return "unknown"
        low_above, upright_below, _, _ = _THRESHOLDS[self.sensitivity]
        angle, shape = torso_angle(pose, self.aspect), body_shape(pose, self.aspect)
        if angle is None:
            return "unknown"
        # Past the threshold the torso is low; a body that is also wider than tall counts a little sooner (a person lying towards the camera has a short torso on screen).
        if angle >= low_above or (shape is not None and shape > 1.3 and angle >= low_above - 10):
            return "low"
        if angle <= upright_below and (shape is None or shape <= 1.3):
            return "upright"
        return "unknown"  # In between (bending, sitting up from lying): neither upright nor low

    def update(self, track_id: str, now: float, pose: Pose | None) -> Fall | None:
        state = self.tracks.setdefault(track_id, _Track())
        _, _, transition, unseen_grace = _THRESHOLDS[self.sensitivity]
        kind = self._classify(pose)
        if kind == "upright":
            state.upright_at = now
            if state.upright_since is None:
                state.upright_since = now
            state.low_since, state.low_ignored = None, False
            if state.fired and now - state.upright_since >= REARM_UPRIGHT_SECONDS:
                state.fired = False
        elif kind == "low":
            state.upright_since = None
            if state.low_since is None and not state.low_ignored:
                # Went low just now: a fall only if they were upright a moment ago.
                if state.upright_at is not None and now - state.upright_at <= transition:
                    state.low_since = now
                else:
                    state.low_ignored = True
            state.seen_at = now
        else:  # Unknown: out of view or in between. A person who was going down and is now hidden stays "down" for the grace period.
            if state.low_since is not None and now - state.seen_at > unseen_grace:
                state.low_since = None
        if state.low_since is not None and not state.fired and now - state.low_since >= self.stay_down_seconds:
            state.fired = True
            return Fall(track_id, state.low_since, now - state.low_since)
        return None

    def tick(self, now: float, seen_tracks: set[str]) -> list[Fall]:
        """For people the camera still follows but the pose could not be made out this time: count the time, so a fall behind furniture is still raised."""
        raised = []
        for track_id, state in self.tracks.items():
            if track_id in seen_tracks or state.low_since is None or state.fired:
                continue
            if now - state.seen_at <= _THRESHOLDS[self.sensitivity][3] and now - state.low_since >= self.stay_down_seconds:
                state.fired = True
                raised.append(Fall(track_id, state.low_since, now - state.low_since))
        return raised

    def forget(self, active: set[str]) -> None:
        for track_id in [key for key in self.tracks if key not in active]:
            del self.tracks[track_id]


# --- settings and the events kept -------------------------------------------------------------------------------------------------------------------------------

def fall_settings(db: sqlite3.Connection, camera_id: str) -> tuple[bool, str, float]:
    """(on, sensitivity, stay-down seconds) for a camera; off when never set."""
    row = db.execute("SELECT enabled,sensitivity,stay_down_seconds FROM fall_settings WHERE camera_id=?", (camera_id,)).fetchone()
    if row is None:
        return False, DEFAULT_SENSITIVITY, DEFAULT_STAY_DOWN_SECONDS
    return bool(row["enabled"]), str(row["sensitivity"]), float(row["stay_down_seconds"])


def camera_aspect(db: sqlite3.Connection, camera_id: str) -> float:
    """The camera's real picture shape (width over height) from its last probe; 16:9 when unknown. The AI worker's frames are squeezed to the detector's size, but body points are
    fractions of the picture, so the angle of a torso is worked out in the camera's own shape."""
    row = db.execute("SELECT video_width,video_height FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if row is not None and row["video_width"] and row["video_height"]:
        return float(row["video_width"]) / float(row["video_height"])
    return 16 / 9


def record_fall(db: sqlite3.Connection, camera_id: str, track_id: str, at: datetime, down_seconds: float, box: dict[str, float]) -> int:
    """Keep a fall and raise an alarm for each ``fall`` rule that watches the camera, inside the caller's transaction. Returns how many alarms were raised."""
    from .rules import fire_fall

    db.execute("INSERT INTO fall_events(id,camera_id,track_id,occurred_at,down_seconds,box_json) VALUES(?,?,?,?,?,?)",
               (str(uuid.uuid4()), camera_id, track_id, at.astimezone(timezone.utc).isoformat(), round(down_seconds, 2), json.dumps(box, separators=(",", ":"))))
    return fire_fall(db, camera_id, track_id, at, box)


def store_fall(camera_id: str, track_id: str, at: datetime, down_seconds: float, box: dict[str, float]) -> int:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            raised = record_fall(db, camera_id, track_id, at, down_seconds, box)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return raised
