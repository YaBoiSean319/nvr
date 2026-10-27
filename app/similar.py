"""Find similar: the tracks that look like a chosen one, across cameras.

Each track's keyframe gets an **appearance embedding**, a vector that is close to another's when the two objects look alike:

- for people, from the OpenVINO ``person-reidentification-retail-0287`` model when its files are installed (a 256-value vector);
- otherwise from the pixels alone (``appearance-colour-v1``): the colours of four bands from head to feet, weighted by how saturated they
  are, plus how light or dark the plain parts are. That tells apart people and vehicles dressed or painted differently, and not
  people dressed alike. It is what an observer would say at a glance, and no more.

Embeddings are stored quantized to one signed byte per value, each with the id of the model that made it, and only embeddings from the same
model and of the same kind of object are compared. The closest ones are found with the ``sqlite-vec`` extension when it can be loaded,
and by scanning in memory when it cannot; both give the same similarity (cosine, from the quantized values), so the same answer.
An embedding is never returned by any API, and goes with its track when footage is deleted. Nothing here names or identifies a person.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .attributes import MIN_REGION_PIXELS, _crop_image

COLOUR_MODEL_ID = "appearance-colour-v1"
REID_MODEL_ID = "person-reidentification-retail-0287"
REID_MODEL_PATH = Path(os.environ.get("NVR_AI_REID_MODEL_PATH", f"/opt/nvr/models/{REID_MODEL_ID}.xml"))
BANDS = 4
HUE_BINS = 8
LEVEL_BINS = 4
SCALE = 127
MAX_RESULTS = 100
EXTENSION_ENABLED = os.environ.get("NVR_SIMILAR_EXTENSION", "on").casefold() not in {"off", "0", "false", "no"}


def colour_embedding(frame: bytes | None, box: dict[str, float]) -> np.ndarray | None:
    """The pixel-colour descriptor of a box, unit length, or None when the box has too few pixels."""
    image = _crop_image(frame, box, (0.0, 1.0, 0.0, 1.0))
    if image.shape[0] < BANDS or image.shape[0] * image.shape[1] < MIN_REGION_PIXELS * BANDS:
        return None
    parts: list[np.ndarray] = []
    for band in np.array_split(image, BANDS, axis=0):
        pixels = band.reshape(-1, 3).astype(np.float32) / 255.0
        blue, green, red = pixels[:, 0], pixels[:, 1], pixels[:, 2]
        top, bottom = np.maximum(np.maximum(blue, green), red), np.minimum(np.minimum(blue, green), red)
        spread = top - bottom
        saturation = np.divide(spread, top, out=np.zeros_like(top), where=top > 0)
        safe = np.where(spread > 0, spread, 1.0)
        hue = np.where(top == red, ((green - blue) / safe) % 6, np.where(top == green, (blue - red) / safe + 2, (red - green) / safe + 4)) * 60.0
        coloured = (top >= 0.2) & (saturation >= 0.18)
        hue_bin = np.minimum((hue / 360.0 * HUE_BINS).astype(int), HUE_BINS - 1)
        hues = np.bincount(hue_bin[coloured], weights=saturation[coloured], minlength=HUE_BINS)
        level_bin = np.minimum((top[~coloured] * LEVEL_BINS).astype(int), LEVEL_BINS - 1)
        levels = np.bincount(level_bin, minlength=LEVEL_BINS).astype(np.float32)
        histogram = np.concatenate([hues, levels]).astype(np.float32)
        total = float(histogram.sum())
        parts.append(np.sqrt(histogram / total) if total > 0 else histogram)  # Hellinger: a distance between colour mixes that cosine can use
    vector = np.concatenate(parts)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else None


class ReidModel:
    """OpenVINO ``person-reidentification-retail-0287``: a person crop (256 high, 128 wide, BGR) in; a 256-value descriptor out."""

    HEIGHT, WIDTH = 256, 128

    def __init__(self, path: Path = REID_MODEL_PATH, provider: str = "CPU", core: Any = None) -> None:
        if core is None:
            import openvino as ov
            core = ov.Core()
        self.compiled = core.compile_model(core.read_model(str(path)), provider)
        self._lock = threading.Lock()  # one inference request at a time: cameras share this model from different threads

    def embed(self, frame: bytes | None, box: dict[str, float]) -> np.ndarray | None:
        image = _crop_image(frame, box, (0.0, 1.0, 0.0, 1.0))
        if image.shape[0] * image.shape[1] < MIN_REGION_PIXELS:
            return None
        rows = np.minimum((np.arange(self.HEIGHT) * image.shape[0] / self.HEIGHT).astype(int), image.shape[0] - 1)
        columns = np.minimum((np.arange(self.WIDTH) * image.shape[1] / self.WIDTH).astype(int), image.shape[1] - 1)
        tensor = image[rows][:, columns].transpose(2, 0, 1)[None].astype(np.float32)
        with self._lock:
            result = self.compiled(tensor)
        values = [np.asarray(value).reshape(-1) for value in result.values()]
        vector = values[0].astype(np.float32) if values else np.empty(0, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        return vector / norm if vector.size and norm > 0 else None


def load_reid_model(path: Path = REID_MODEL_PATH, provider: str = "CPU") -> ReidModel | None:
    """The person re-identification model if its files are installed and it loads, else None (the colour descriptor is used)."""
    if not path.is_file() or not path.with_suffix(".bin").is_file():
        return None
    try:
        return ReidModel(path, provider)
    except Exception:  # noqa: BLE001 - a model that will not load must never stop detection
        return None


def embed(frame: bytes | None, label: str, box: dict[str, float], model: Any = None) -> tuple[str, np.ndarray] | None:
    """``(model id, unit vector)`` for one detection, or None when it cannot be described. People use the re-id model when there is one."""
    if label == "person" and model is not None:
        try:
            vector = model.embed(frame, box)
        except Exception:  # noqa: BLE001 - fall back to the pixels
            vector = None
        if vector is not None:
            return REID_MODEL_ID, vector
    vector = colour_embedding(frame, box)
    return (COLOUR_MODEL_ID, vector) if vector is not None else None


def quantize(vector: np.ndarray) -> bytes:
    """A unit vector as signed bytes, one for each value."""
    return np.clip(np.round(vector * SCALE), -SCALE, SCALE).astype(np.int8).tobytes()


def cosine(left: bytes, right: bytes) -> float:
    """The cosine similarity of two quantized vectors: 1 for the same direction, 0 for unrelated, negative for opposite."""
    a, b = np.frombuffer(left, dtype=np.int8).astype(np.float64), np.frombuffer(right, dtype=np.int8).astype(np.float64)
    if a.shape != b.shape:
        return 0.0
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / norm if norm > 0 else 0.0


def store_embedding(db: sqlite3.Connection, track_id: str, model_id: str, vector: np.ndarray) -> None:
    db.execute("INSERT OR REPLACE INTO track_embeddings(track_id,model_id,dim,vector,created_at) VALUES(?,?,?,?,?)",
               (track_id, model_id, int(vector.size), quantize(vector), datetime.now(timezone.utc).isoformat()))


def load_extension(db: sqlite3.Connection) -> bool:
    """Make ``sqlite-vec`` available on this connection. False when it is switched off, not installed or will not load."""
    if not EXTENSION_ENABLED:
        return False
    try:
        db.execute("SELECT vec_version()")
        return True
    except sqlite3.OperationalError:
        pass
    try:
        import sqlite_vec
        db.enable_load_extension(True)
        try:
            sqlite_vec.load(db)
        finally:
            db.enable_load_extension(False)
        db.execute("SELECT vec_version()")
        return True
    except Exception:  # noqa: BLE001 - the scan below gives the same answer
        return False


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def find_similar(db: sqlite3.Connection, track_id: str, camera_ids: list[str], start: datetime, end: datetime, *, limit: int = 30,
                 min_similarity: float = 0.6, use_extension: bool | None = None) -> list[tuple[str, float]]:
    """``(track id, similarity)`` for the tracks most like ``track_id``, closest first: same kind of object, same model, on these cameras, in range.

    Raises ``LookupError`` when the track has no embedding. ``use_extension`` forces the extension (True) or the scan (False); left alone, the
    extension is used when it loads."""
    source = db.execute("SELECT e.model_id,e.dim,e.vector,t.label FROM track_embeddings e JOIN ai_tracks t ON t.id=e.track_id WHERE e.track_id=?", (track_id,)).fetchone()
    if source is None:
        raise LookupError("This object has no appearance to compare")
    if not camera_ids:
        return []
    limit = max(1, min(limit, MAX_RESULTS))
    where = (f"e.model_id=? AND e.dim=? AND t.label=? AND t.id!=? AND t.camera_id IN ({','.join('?' * len(camera_ids))}) AND t.last_seen_at>=? AND t.first_seen_at<?")
    values: list[Any] = [source["model_id"], source["dim"], source["label"], track_id, *camera_ids, _iso(start), _iso(end)]
    vector = bytes(source["vector"])
    if use_extension is not False and load_extension(db):
        rows = db.execute(
            f"SELECT id,similarity FROM (SELECT t.id AS id,t.first_seen_at AS first_seen,ROUND(1.0-vec_distance_cosine(vec_int8(e.vector),vec_int8(?)),5) AS similarity "
            f"FROM track_embeddings e JOIN ai_tracks t ON t.id=e.track_id WHERE {where}) WHERE similarity>=? ORDER BY similarity DESC,first_seen,id LIMIT ?",
            (vector, *values, min_similarity, limit)).fetchall()
        return [(row["id"], float(row["similarity"])) for row in rows]
    if use_extension is True:
        raise RuntimeError("The sqlite-vec extension is not available")
    scored = [(row["id"], round(cosine(vector, bytes(row["vector"])), 5), row["first_seen_at"]) for row in db.execute(
        f"SELECT t.id,t.first_seen_at,e.vector FROM track_embeddings e JOIN ai_tracks t ON t.id=e.track_id WHERE {where}", values)]
    kept = sorted((item for item in scored if item[1] >= min_similarity), key=lambda item: (-item[1], item[2], item[0]))[:limit]
    return [(track, similarity) for track, similarity, _ in kept]
