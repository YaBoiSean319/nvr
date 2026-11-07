"""Looking for watched faces on the cameras switched on for it (#253, part 3).

Like the licence-plate scanner, it gives each person the AI is following on one camera a few attempts, never more than one picture at a time, and never holds analysis up:
a full-size picture of the camera is taken, the faces in it are found and described, and a face that is on a followed person and is alike enough to someone on the watch list is
kept as a sighting, which raises the alarm of any ``face`` rule that watches the camera. **A face that matches nobody on the list is dropped: nothing about it is stored.** A face
inside a privacy mask is not read at all. With an empty list the scanner does nothing, and no picture is taken.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Sequence

import numpy as np

from . import face_models, faces, plates
from .store import connect

logger = logging.getLogger("nvr.faces")

SETTINGS_REFRESH = 30.0
ATTEMPT_INTERVAL = 3.0  # Seconds between pictures for one camera
MAX_ATTEMPTS = 8  # Pictures spent on one person before giving up on them
PERSON_MARGIN = 0.1  # A face counts as on a person when its centre is in the person's box grown by this fraction


def associate(face_box: tuple[float, float, float, float], people: Sequence[tuple[str, dict[str, float]]]) -> str | None:
    """The followed person a face is on: the one whose (slightly grown) box holds the face's centre, the smallest box if several."""
    cx, cy = (face_box[0] + face_box[2]) / 2, (face_box[1] + face_box[3]) / 2
    best: tuple[float, str] | None = None
    for track_id, box in people:
        mx, my = box["width"] * PERSON_MARGIN, box["height"] * PERSON_MARGIN
        if box["x"] - mx <= cx <= box["x"] + box["width"] + mx and box["y"] - my <= cy <= box["y"] + box["height"] + my:
            area = box["width"] * box["height"]
            if best is None or area < best[0]:
                best = (area, track_id)
    return best[1] if best else None


class FaceScanner:
    """One camera's attempts. ``snapshot`` gives a full-size BGR picture of the camera now (or None); ``watched`` gives the list as (entry id, embeddings) pairs and may be empty;
    ``store`` is called, in a thread, with ``(track_id, entry_id, similarity, face, picture, taken_at)`` for each match. It knows nothing of databases or streams, so tests drive it
    with a stand-in reader."""

    def __init__(self, reader: Any, snapshot: Callable[[], Awaitable[np.ndarray | None]], watched: Callable[[], list[tuple[str, list[np.ndarray]]]], store: Callable[..., Any], *,
                 threshold: float = face_models.DEFAULT_MATCH_THRESHOLD, interval: float = ATTEMPT_INTERVAL, max_attempts: int = MAX_ATTEMPTS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.reader, self.snapshot, self.watched, self.store = reader, snapshot, watched, store
        self.threshold, self.interval, self.max_attempts, self.clock = threshold, interval, max_attempts, clock
        self.attempts: dict[str, int] = {}
        self.done: set[str] = set()
        self.people: dict[str, dict[str, float]] = {}
        self.last = float("-inf")
        self.running = False

    def seen(self, track_id: str, label: str, box: dict[str, float]) -> None:
        if label == "person":
            self.people[track_id] = box

    def _wanted(self) -> dict[str, dict[str, float]]:
        return {track_id: box for track_id, box in self.people.items() if track_id not in self.done and self.attempts.get(track_id, 0) < self.max_attempts}

    def due(self) -> bool:
        return bool(self._wanted()) and not self.running and self.clock() - self.last >= self.interval

    async def scan(self) -> int:
        """One attempt for every person still unmatched; returns how many sightings were kept."""
        if not self.due():
            return 0
        self.running, self.last = True, self.clock()
        kept = 0
        try:
            watching = await asyncio.to_thread(self.watched)
            if not watching:
                return 0  # Nobody to look for: no picture is taken
            wanted = self._wanted()
            for track_id in wanted:
                self.attempts[track_id] = self.attempts.get(track_id, 0) + 1
            picture = await self.snapshot()
            if picture is None:
                return 0
            taken = datetime.now(timezone.utc)
            for face in await asyncio.to_thread(self.reader.faces, picture):
                track_id = associate(face.box, wanted.items())
                if track_id is None:
                    continue  # Not on anyone the AI is following: not looked at further
                found = face_models.match(face.embedding, watching, self.threshold)
                if found is None:
                    continue  # Matches nobody on the list: dropped, nothing kept
                await asyncio.to_thread(self.store, track_id, found[0], found[1], face, picture, taken)
                self.done.add(track_id)
                kept += 1
        except Exception:  # noqa: BLE001 - a picture or read that fails costs this attempt, never the camera's analysis
            logger.exception("A face attempt failed")
        finally:
            self.running = False
        return kept

    def forget(self, live: set[str]) -> None:
        for table in (self.people, self.attempts):
            for track_id in [key for key in table if key not in live]:
                del table[track_id]
        self.done &= live


def keep_sighting(camera_id: str, track_id: str, entry_id: str, similarity: float, face: face_models.Face, picture: np.ndarray, at: datetime) -> bool:
    """Keep a match (and raise the alarms of the rules that watch for it) in its own transaction; False when it is not kept (in a privacy mask, or the entry or track has gone)."""
    from . import privacy, rules

    box = {"x": face.box[0], "y": face.box[1], "width": face.box[2] - face.box[0], "height": face.box[3] - face.box[1]}
    with connect() as db:
        if plates.overlaps_privacy_zone(box, privacy.server_zones(db, camera_id, at)):
            return False
    small = faces.picture_of(picture, face)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            entry = db.execute("SELECT enabled,expires_at FROM face_watch_entries WHERE id=?", (entry_id,)).fetchone()
            live = entry is not None and entry["enabled"] and (entry["expires_at"] is None or entry["expires_at"] > at.isoformat())
            if not live or db.execute("SELECT 1 FROM ai_tracks WHERE id=?", (track_id,)).fetchone() is None:
                db.execute("ROLLBACK")
                return False
            db.execute("INSERT INTO face_sightings(id,entry_id,camera_id,track_id,seen_at,similarity,box_json,picture) VALUES(?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), entry_id, camera_id, track_id, at.isoformat(), round(float(similarity), 4), json.dumps(box, separators=(",", ":")), small))
            rules.fire_face(db, camera_id, track_id, entry_id, at, box)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return True


def scanning(db: sqlite3.Connection, camera_id: str) -> tuple[bool, float]:
    """Whether this camera looks for faces (switched on, with someone on the list) and how alike a face must be."""
    row = db.execute("SELECT enabled FROM face_camera_settings WHERE camera_id=?", (camera_id,)).fetchone()
    on = bool(row and row["enabled"]) and db.execute("SELECT 1 FROM face_watch_entries WHERE enabled=1 LIMIT 1").fetchone() is not None
    return on, faces.match_threshold(db)


def watch_list() -> list[tuple[str, list[np.ndarray]]]:
    with connect() as db:
        return faces.watched(db)
