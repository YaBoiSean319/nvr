"""The face watch list (#253): who is on it, which cameras look for faces, and finding a face in recorded video to put on it.

Everything here is administrators only and audited by the API (``app/api/faces.py``). An entry is a person someone chose to watch for, with the reason and an optional expiry;
what is kept of them is the numbers that describe their face (``face_embeddings``) and a small picture of the face the entry was made from. A face that matches nothing on the
list is never kept. Looking for faces on cameras, and the alarm, are the next parts (docs/face-watch.md).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from typing import Any

import numpy as np

from . import face_models, plates, privacy
from .store import connect
from app.clock import now as _now, parse as _parse

MARGIN = 0.3  # How much of the face's own size is kept around it in the entry's picture
MIN_IOU = 0.3  # How well a clicked box must fit a detected face to pick it
EXTRACT_TIMEOUT = 20.0


class FaceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


_reader: face_models.FaceReader | None = None
_reader_failed = False


def reader() -> face_models.FaceReader:
    """The face reader, loaded the first time it is needed (the models are 39 MB); a FaceError says why when it cannot be had."""
    global _reader, _reader_failed
    if _reader is None:
        if not face_models.installed():
            raise FaceError(503, "The face models are not installed on this server")
        loaded = None if _reader_failed else face_models.load_reader()
        if loaded is None:
            _reader_failed = True
            raise FaceError(503, "The face models could not be loaded")
        _reader = loaded
    return _reader


# --- settings ----------------------------------------------------------------------------------------------------------------------------

def match_threshold(db: sqlite3.Connection) -> float:
    row = db.execute("SELECT match_threshold FROM face_settings WHERE singleton_id=1").fetchone()
    return float(row["match_threshold"]) if row else face_models.DEFAULT_MATCH_THRESHOLD


def cameras_enabled(db: sqlite3.Connection) -> list[str]:
    return [row["camera_id"] for row in db.execute("SELECT camera_id FROM face_camera_settings WHERE enabled=1 ORDER BY camera_id")]


# --- the list ----------------------------------------------------------------------------------------------------------------------------

def _entry_view(row: sqlite3.Row, count: int, now: datetime) -> dict[str, Any]:
    expires = row["expires_at"]
    return {"id": row["id"], "name": row["name"], "reason": row["reason"], "expires_at": expires,
            "expired": bool(expires and _parse(expires) <= now), "enabled": bool(row["enabled"]), "embeddings": count,
            "has_picture": row["picture"] is not None, "created_by": row["created_by_name"], "created_at": row["created_at"], "updated_at": row["updated_at"]}


def list_entries(db: sqlite3.Connection, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or _now()
    rows = db.execute(
        "SELECT e.*, u.username AS created_by_name, (SELECT COUNT(*) FROM face_embeddings WHERE entry_id=e.id) AS embeddings "
        "FROM face_watch_entries e LEFT JOIN users u ON u.id=e.created_by ORDER BY e.created_at,e.id").fetchall()
    return [_entry_view(row, int(row["embeddings"]), now) for row in rows]


def get_entry(db: sqlite3.Connection, entry_id: str, now: datetime | None = None) -> dict[str, Any]:
    row = db.execute("SELECT e.*, u.username AS created_by_name, (SELECT COUNT(*) FROM face_embeddings WHERE entry_id=e.id) AS embeddings "
                     "FROM face_watch_entries e LEFT JOIN users u ON u.id=e.created_by WHERE e.id=?", (entry_id,)).fetchone()
    if row is None:
        raise FaceError(404, "That entry is not on the list")
    return _entry_view(row, int(row["embeddings"]), now or _now())


def watched(db: sqlite3.Connection, now: datetime | None = None) -> list[tuple[str, list[np.ndarray]]]:
    """What a camera compares faces with: each entry that is switched on and has not expired, with its embeddings."""
    now_text = (now or _now()).isoformat()
    result = []
    for row in db.execute("SELECT id FROM face_watch_entries WHERE enabled=1 AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at,id", (now_text,)):
        embeddings = [np.frombuffer(item["embedding"], dtype=np.float32).astype(np.float64)
                      for item in db.execute("SELECT embedding FROM face_embeddings WHERE entry_id=? ORDER BY id", (row["id"],))]
        if embeddings:
            result.append((row["id"], embeddings))
    return result


def entry_picture(db: sqlite3.Connection, entry_id: str) -> bytes:
    row = db.execute("SELECT picture FROM face_watch_entries WHERE id=?", (entry_id,)).fetchone()
    if row is None or row["picture"] is None:
        raise FaceError(404, "That entry has no picture")
    return bytes(row["picture"])


# --- finding a face in recorded video ----------------------------------------------------------------------------------------------------

async def frame_at(camera_id: str, at: datetime) -> np.ndarray:
    """The recorded picture of a camera at a moment, at the recording's own size and unmasked (masks are checked against the faces found, not drawn)."""
    at = at.astimezone(timezone.utc)
    with connect() as db:
        row = db.execute("SELECT relative_path,starts_at FROM recording_segments WHERE camera_id=? AND starts_at<=? AND ends_at>? ORDER BY starts_at DESC LIMIT 1",
                         (camera_id, at.isoformat(), at.isoformat())).fetchone()
    if row is None:
        raise FaceError(404, "There is no recording of that camera at that time")
    if not shutil.which("ffmpeg"):
        raise FaceError(503, "Extracting a picture needs FFmpeg, which is not available")
    from .retention import acquire_playback, release_playback
    from .storage import recording_file_path
    try:
        path = await asyncio.to_thread(recording_file_path, row["relative_path"])
    except (OSError, ValueError):
        raise FaceError(410, "That recording is unavailable") from None
    if not path.is_file():
        raise FaceError(410, "That recording is unavailable")
    acquire_playback(path)
    try:
        offset = max(0.0, (at - _parse(row["starts_at"])).total_seconds())
        process = await asyncio.create_subprocess_exec(
            shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{offset:.3f}", "-i", str(path), "-frames:v", "1",
            "-f", "image2pipe", "-c:v", "bmp", "-pix_fmt", "bgr24", "pipe:1", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            data, _ = await asyncio.wait_for(process.communicate(), EXTRACT_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise FaceError(503, "Extracting the picture took too long") from None
    finally:
        release_playback(path)
    frame = plates.decode_bmp(data)
    if frame is None:
        raise FaceError(422, "A picture could not be taken from that recording")
    return np.ascontiguousarray(frame)


def _inside(zone: dict[str, float], x: float, y: float) -> bool:
    return zone["x"] <= x <= zone["x"] + zone["width"] and zone["y"] <= y <= zone["y"] + zone["height"]


def _masked(zones: list[dict[str, float]], face: face_models.Face) -> bool:
    """A face is in a privacy mask when its centre or any landmark is: the NVR does not read faces from areas an administrator chose to hide."""
    x1, y1, x2, y2 = face.box
    return any(_inside(zone, (x1 + x2) / 2, (y1 + y2) / 2) or any(_inside(zone, x, y) for x, y in face.landmarks) for zone in zones)


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


async def find_faces(camera_id: str, at: datetime) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """The picture at a moment and the faces in it that can be used: (picture, [{box, score, pixels, usable, reason}])."""
    frame = await frame_at(camera_id, at)
    with connect() as db:
        zones = privacy.server_zones(db, camera_id, at.astimezone(timezone.utc))
    faces = await asyncio.to_thread(lambda: reader().faces(frame))
    found = []
    for face in faces:
        masked = _masked(zones, face)
        x1, y1, x2, y2 = face.box
        found.append({"box": {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}, "score": face.score, "pixels": list(face.pixels), "usable": not masked,
                      "reason": "This face is inside a privacy mask" if masked else None, "_face": face})
    return frame, found


def choose(found: list[dict[str, Any]], box: dict[str, float] | None, point: dict[str, float] | None) -> dict[str, Any]:
    """The detected face a person picked, by the box they drew (best overlap) or the point they clicked (the smallest face under it)."""
    if box is not None:
        wanted = (box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"])
        scored = [(_iou(wanted, item["_face"].box), item) for item in found]
        best = max(scored, key=lambda pair: pair[0], default=(0.0, None))
        if best[1] is None or best[0] < MIN_IOU:
            raise FaceError(422, "No face was found where you chose; try another picture of them")
        return best[1]
    assert point is not None
    under = [item for item in found if item["_face"].box[0] <= point["x"] <= item["_face"].box[2] and item["_face"].box[1] <= point["y"] <= item["_face"].box[3]]
    if not under:
        raise FaceError(422, "No face was found where you chose; try another picture of them")
    return min(under, key=lambda item: (item["_face"].box[2] - item["_face"].box[0]) * (item["_face"].box[3] - item["_face"].box[1]))


def frame_jpeg(frame: np.ndarray, width: int = 960) -> bytes | None:
    """The whole picture as a JPEG no wider than `width`, for choosing a face on it."""
    ffmpeg = shutil.which("ffmpeg")
    if frame.ndim != 3 or frame.size == 0 or not ffmpeg:
        return None
    height, columns = frame.shape[:2]
    try:
        result = subprocess.run([ffmpeg, "-v", "error", "-threads", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{columns}x{height}", "-i", "pipe:0",
                                 "-vf", f"scale='min({width},iw)':-2:flags=bicubic", "-frames:v", "1", "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                                input=np.ascontiguousarray(frame, dtype=np.uint8).tobytes(), capture_output=True, timeout=10, check=False)
    except subprocess.TimeoutExpired:
        return None
    return result.stdout if result.returncode == 0 and result.stdout.startswith(b"\xff\xd8") else None


def picture_of(frame: np.ndarray, face: face_models.Face) -> bytes | None:
    """A small JPEG of the face (with a margin) to show who an entry is."""
    rows, columns = frame.shape[:2]
    x1, y1, x2, y2 = face.box
    pad_x, pad_y = (x2 - x1) * MARGIN, (y2 - y1) * MARGIN
    left, right = int(max(0.0, x1 - pad_x) * columns), int(min(1.0, x2 + pad_x) * columns)
    top, bottom = int(max(0.0, y1 - pad_y) * rows), int(min(1.0, y2 + pad_y) * rows)
    return plates.encode_crop(frame[top:bottom, left:right])


def add_embedding(db: sqlite3.Connection, entry_id: str, face: face_models.Face, camera_id: str, at: datetime) -> None:
    db.execute("INSERT INTO face_embeddings(entry_id,embedding,camera_id,source_at) VALUES(?,?,?,?)",
               (entry_id, np.asarray(face.embedding, dtype=np.float32).tobytes(), camera_id, at.astimezone(timezone.utc).isoformat()))


def create_entry(db: sqlite3.Connection, name: str, reason: str, expires_at: str | None, picture: bytes | None, user_id: int | None) -> str:
    entry_id = str(uuid.uuid4())
    db.execute("INSERT INTO face_watch_entries(id,name,reason,expires_at,picture,created_by) VALUES(?,?,?,?,?,?)", (entry_id, name, reason, expires_at, picture, user_id))
    return entry_id


def audit(db: sqlite3.Connection, user_id: int, action: str, resource_id: str, details: dict[str, Any]) -> None:
    db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
               (user_id, action, "face_watch", resource_id, json.dumps(details, separators=(",", ":"))))
