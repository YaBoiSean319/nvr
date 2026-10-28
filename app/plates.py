"""Licence plates: reading them on designated cameras, keeping the reads with their tracks, searching them, and watch lists.

**Reading.** A camera is designated for plates (``plate_settings``). While the AI worker analyses it, each vehicle track gets a few attempts at a plate:
a full-size snapshot of the camera is taken, an OpenVINO plate detector finds plates in it, an OpenVINO recogniser reads each plate crop, and a plate that
sits on a tracked vehicle is stored with that track, its confidence and the crop. Both models are installed by the administrator beside the others (they
are not bundled; see ``docs/licence-plates.md``). Without them nothing is read, and the settings page says so.

**Plates are compared in a normal form**: upper case, letters and digits only (``ab-12 cde`` is ``AB12CDE``). ``folded`` further makes look-alike
characters one (``O``/``Q``/``D`` and ``0``, ``I``/``L`` and ``1``, ``Z`` and ``2``, ``S`` and ``5``, ``B`` and ``8``) so a search can forgive what cameras
commonly get wrong.

**Patterns** use ``*`` for any run of characters and ``?`` for exactly one. A search without a wildcard finds plates that *contain* the text (a partial
plate); with a wildcard the pattern must match the whole plate (``AB12*`` starts with, ``*CDE`` ends with, ``AB??CDE`` is anchored). A watch list entry
is always a whole-plate pattern.

**Watch lists** are *deny* (a plate on the list is of interest) or *allow* (a plate not on the list is). Entries can expire. A ``plate`` rule names a list:
on a deny list it fires when a read matches an entry; on an allow list it fires when a read matches no entry, and a plate that is on the list does
nothing. Recognition quality depends on the camera and the site and is reported (confidence, accuracy on a labelled sample), not asserted.
"""
from __future__ import annotations

import asyncio
import csv
import fnmatch
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import numpy as np

logger = logging.getLogger("nvr.plates")

DETECTOR_ID = "vehicle-license-plate-detection-barrier-0106"
RECOGNISER_ID = "license-plate-recognition-barrier-0007"
DETECTOR_PATH = Path(os.environ.get("NVR_PLATE_DETECTOR_MODEL_PATH", f"/opt/nvr/models/{DETECTOR_ID}.xml"))
RECOGNISER_PATH = Path(os.environ.get("NVR_PLATE_RECOGNISER_MODEL_PATH", f"/opt/nvr/models/{RECOGNISER_ID}.xml"))
DEFAULT_CHARSET = tuple("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
PLATE_LABEL = 2  # The detector's class for a plate (1 is a vehicle).
DETECTOR_SIZE = 300
DETECTION_THRESHOLD = 0.5
MAX_PLATE_LENGTH = 12
MIN_PLATE_LENGTH = 2
MAX_PATTERN_LENGTH = 16
SNAPSHOT_WIDTH, SNAPSHOT_HEIGHT = 1280, 720
ATTEMPT_INTERVAL = 3.0  # Seconds between snapshots for the same camera.
MAX_ATTEMPTS = 4  # Snapshots tried for one vehicle before giving up on it.
SETTINGS_REFRESH = 30.0
VEHICLE_MARGIN = 0.15  # A plate counts as on a vehicle when its centre is in the vehicle's box grown by this fraction.

_FOLD = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8"})
_PATTERN_ALLOWED = re.compile(r"[^A-Z0-9*?]")
_PLATE_ALLOWED = re.compile(r"[^A-Z0-9]")


# --- text -------------------------------------------------------------------------------------------------------------------------------------

def normalise(text: str) -> str:
    """A plate in its normal form: upper case, letters and digits only."""
    return _PLATE_ALLOWED.sub("", text.upper())[:MAX_PLATE_LENGTH]


def fold(text: str) -> str:
    """The plate with look-alike characters made one; wildcards are left alone."""
    return text.translate(_FOLD)


def normalise_pattern(text: str) -> str:
    """A search or watch list pattern in its normal form (letters, digits, ``*`` and ``?``), with runs of ``*`` made one."""
    pattern = re.sub(r"\*{2,}", "*", _PATTERN_ALLOWED.sub("", text.upper()))
    return pattern[:MAX_PATTERN_LENGTH]


def has_wildcard(pattern: str) -> bool:
    return "*" in pattern or "?" in pattern


def search_glob(text: str) -> str | None:
    """The whole-plate pattern a search for ``text`` means: a partial plate is wrapped in ``*``, a pattern with wildcards stands as written.

    None when nothing searchable is left (the text had no letters, digits or wildcards)."""
    pattern = normalise_pattern(text)
    if not pattern:
        return None
    return pattern if has_wildcard(pattern) else f"*{pattern}*"


def matches(pattern: str, plate: str, lookalikes: bool = False) -> bool:
    """Whether a whole-plate pattern matches a plate (both in normal form)."""
    if lookalikes:
        pattern, plate = fold(pattern), fold(plate)
    return fnmatch.fnmatchcase(plate, pattern)


def valid_plate(text: str) -> bool:
    return MIN_PLATE_LENGTH <= len(text) <= MAX_PLATE_LENGTH


# --- the models -------------------------------------------------------------------------------------------------------------------------------

@dataclass
class Plate:
    text: str
    confidence: float
    box: dict[str, float]  # x, y, width, height as fractions of the frame
    crop: np.ndarray | None = field(default=None, repr=False)


def models_available(detector: Path = DETECTOR_PATH, recogniser: Path = RECOGNISER_PATH) -> bool:
    return all(path.is_file() and path.with_suffix(".bin").is_file() for path in (detector, recogniser))


def read_charset(recogniser: Path) -> tuple[tuple[str, ...], int | None]:
    """The recogniser's symbols, in class order, and which class is the CTC blank (None when it has none).

    ``<recogniser>.charset.txt`` holds one symbol per line; a line ``<blank>`` marks the blank. Without the file the symbols are 0-9 and A-Z with the
    blank last, which is what a Latin plate recogniser usually emits."""
    sidecar = recogniser.with_suffix(".charset.txt")
    if sidecar.is_file():
        symbols = tuple(line.rstrip("\n") for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip("\n") != "")
        blank = next((index for index, symbol in enumerate(symbols) if symbol == "<blank>"), None)
        return symbols, blank
    return DEFAULT_CHARSET + ("<blank>",), len(DEFAULT_CHARSET)


def decode_plate(output: np.ndarray, symbols: tuple[str, ...], blank: int | None) -> tuple[str, float | None]:
    """The text a recogniser's output spells, and its confidence (None when the output carries none).

    Two shapes are understood: *scores* (an output with an axis as long as the symbol list, flattening to positions x classes; each position's best class,
    repeats collapsed and the blank dropped, as CTC does) and *indices* (whole numbers, one per position, ``-1`` ending the text, as the Open Model Zoo
    plate recognisers emit)."""
    flat = np.asarray(output, dtype=np.float32).reshape(-1)
    classes = len(symbols)
    if flat.size and classes not in np.shape(output) and np.all(flat == np.round(flat)) and flat.min() >= -1 and flat.max() < classes:
        text: list[str] = []
        for index in flat.astype(int):
            if index < 0:
                break
            if index != blank:
                text.append(symbols[index])
        return "".join(text), None
    if flat.size == 0 or flat.size % classes:
        raise ValueError("The recogniser's output does not fit its symbols")
    scores = flat.reshape(-1, classes)
    if not np.allclose(scores.sum(axis=1), 1.0, atol=1e-2):
        shifted = np.exp(scores - scores.max(axis=1, keepdims=True))
        scores = shifted / shifted.sum(axis=1, keepdims=True)
    best, strength = scores.argmax(axis=1), scores.max(axis=1)
    text, kept, previous = [], [], None
    for position, index in enumerate(best):
        if index != previous and index != blank:
            text.append(symbols[int(index)])
            kept.append(float(strength[position]))
        previous = index
    return "".join(text), (float(np.mean(kept)) if kept else 0.0)


def _resize(image: np.ndarray, height: int, width: int) -> np.ndarray:
    rows = np.minimum((np.arange(height) * image.shape[0] / height).astype(int), image.shape[0] - 1)
    columns = np.minimum((np.arange(width) * image.shape[1] / width).astype(int), image.shape[1] - 1)
    return image[rows][:, columns]


class PlateReader:
    """OpenVINO plate detector and recogniser. ``read`` takes a BGR frame (rows x columns x 3) and returns the plates found, with their text.

    Detector contract: input ``[1,3,300,300]`` BGR; output ``[1,1,N,7]`` rows ``image_id, label, confidence, x1, y1, x2, y2`` (fractions), label 2 a plate.
    Recogniser contract: input ``[1,3,H,W]`` BGR (the size is read from the model); output as ``decode_plate`` describes."""

    reader_id = f"{DETECTOR_ID}+{RECOGNISER_ID}"

    def __init__(self, detector: Path = DETECTOR_PATH, recogniser: Path = RECOGNISER_PATH, provider: str = "CPU", core: Any = None) -> None:
        if core is None:
            import openvino as ov
            core = ov.Core()
        self.detector = core.compile_model(core.read_model(str(detector)), provider)
        self.recogniser = core.compile_model(core.read_model(str(recogniser)), provider)
        self.symbols, self.blank = read_charset(recogniser)
        shape = list(self.recogniser.input(0).shape)
        self.input_height, self.input_width = int(shape[-2]), int(shape[-1])

    def _detect(self, frame: np.ndarray) -> list[tuple[float, tuple[float, float, float, float]]]:
        tensor = _resize(frame, DETECTOR_SIZE, DETECTOR_SIZE).transpose(2, 0, 1)[None].astype(np.float32)
        rows = np.asarray(next(iter(self.detector(tensor).values()))).reshape(-1, 7)
        found = []
        for _, label, confidence, x1, y1, x2, y2 in rows:
            if int(label) == PLATE_LABEL and confidence >= DETECTION_THRESHOLD and x2 > x1 and y2 > y1:
                found.append((float(confidence), (min(max(float(x1), 0.0), 1.0), min(max(float(y1), 0.0), 1.0), min(max(float(x2), 0.0), 1.0), min(max(float(y2), 0.0), 1.0))))
        return found

    def read(self, frame: np.ndarray) -> list[Plate]:
        height, width = frame.shape[:2]
        plates: list[Plate] = []
        for detected, (x1, y1, x2, y2) in self._detect(frame):
            left, right, top, bottom = int(x1 * width), max(int(x1 * width) + 1, int(round(x2 * width))), int(y1 * height), max(int(y1 * height) + 1, int(round(y2 * height)))
            crop = frame[top:bottom, left:right]
            if crop.size == 0:
                continue
            tensor = _resize(crop, self.input_height, self.input_width).transpose(2, 0, 1)[None].astype(np.float32)
            try:
                text, confidence = decode_plate(np.asarray(next(iter(self.recogniser(tensor).values()))), self.symbols, self.blank)
            except ValueError:
                continue
            text = normalise(text)
            if not valid_plate(text):
                continue
            plates.append(Plate(text, round(min(1.0, detected if confidence is None else min(detected, confidence)), 3),
                                {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}, crop))
        return plates


def load_reader(provider: str = "CPU") -> PlateReader | None:
    """The plate reader if both models are installed and load, else None (nothing is then read)."""
    if not models_available():
        return None
    try:
        return PlateReader(provider=provider)
    except Exception:  # noqa: BLE001 - models that will not load must never stop detection
        logger.exception("The plate models could not be loaded")
        return None


def model_status() -> dict[str, Any]:
    return {"available": models_available(), "detector": DETECTOR_ID, "recogniser": RECOGNISER_ID,
            "detector_installed": DETECTOR_PATH.is_file() and DETECTOR_PATH.with_suffix(".bin").is_file(),
            "recogniser_installed": RECOGNISER_PATH.is_file() and RECOGNISER_PATH.with_suffix(".bin").is_file()}


def associate(box: dict[str, float], vehicles: Iterable[tuple[str, dict[str, float]]]) -> str | None:
    """The track of the smallest vehicle (box grown by ``VEHICLE_MARGIN``) the plate's centre is in, or None when it is on none."""
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    best: tuple[float, str] | None = None
    for track_id, vehicle in vehicles:
        mx, my = vehicle["width"] * VEHICLE_MARGIN, vehicle["height"] * VEHICLE_MARGIN
        if vehicle["x"] - mx <= cx <= vehicle["x"] + vehicle["width"] + mx and vehicle["y"] - my <= cy <= vehicle["y"] + vehicle["height"] + my:
            area = vehicle["width"] * vehicle["height"]
            if best is None or area < best[0]:
                best = (area, track_id)
    return best[1] if best else None


# --- storage ----------------------------------------------------------------------------------------------------------------------------------

def encode_crop(crop: np.ndarray | None) -> bytes | None:
    """A BGR crop as a JPEG (ffmpeg), or None when it cannot be encoded."""
    ffmpeg = shutil.which("ffmpeg")
    if crop is None or crop.ndim != 3 or crop.size == 0 or not ffmpeg:
        return None
    height, width = crop.shape[:2]
    try:
        result = subprocess.run([ffmpeg, "-v", "error", "-threads", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-i", "pipe:0",
                                 "-vf", "scale=iw*max(1\\,ceil(160/iw)):-1:flags=bicubic", "-frames:v", "1", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                                input=np.ascontiguousarray(crop, dtype=np.uint8).tobytes(), capture_output=True, timeout=5, check=False)
    except subprocess.TimeoutExpired:
        return None
    return result.stdout if result.returncode == 0 and result.stdout.startswith(b"\xff\xd8") else None


def store_read(db: sqlite3.Connection, camera_id: str, track_id: str, plate: str, confidence: float, at: datetime, box: dict[str, float], crop: bytes | None,
               reader_id: str) -> tuple[str, bool]:
    """Keep a read inside the caller's transaction, once for each plate on a track (the most confident read, and its crop, is the one kept).

    Returns the read's id and whether the plate is new on the track. A new plate raises the ``plate`` rules."""
    stamp = at.astimezone(timezone.utc).isoformat()
    existing = db.execute("SELECT id,confidence FROM plate_reads WHERE track_id=? AND plate=?", (track_id, plate)).fetchone()
    if existing:
        if confidence > existing["confidence"]:
            db.execute("UPDATE plate_reads SET confidence=?,read_at=?,box_json=?,crop=COALESCE(?,crop),reader=? WHERE id=?",
                       (confidence, stamp, json.dumps(box, separators=(",", ":")), crop, reader_id, existing["id"]))
        return existing["id"], False
    read_id = str(uuid.uuid4())
    db.execute("INSERT INTO plate_reads(id,camera_id,track_id,plate,folded,confidence,read_at,box_json,crop,reader) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (read_id, camera_id, track_id, plate, fold(plate), confidence, stamp, json.dumps(box, separators=(",", ":")), crop, reader_id))
    from .rules import fire_plate
    fire_plate(db, camera_id, track_id, plate, at, box)
    return read_id, True


# --- watch lists ------------------------------------------------------------------------------------------------------------------------------

def _live(entry: sqlite3.Row, now: datetime) -> bool:
    if not entry["expires_at"]:
        return True
    expires = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
    return (expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)) > now


def list_matches(db: sqlite3.Connection, list_id: str, plate: str, now: datetime | None = None) -> bool | None:
    """Whether a plate is on the watch list (an entry that has not expired matches it); None when there is no such list."""
    watch = db.execute("SELECT kind,lookalikes FROM plate_watch_lists WHERE id=?", (list_id,)).fetchone()
    if watch is None:
        return None
    moment = now or datetime.now(timezone.utc)
    return any(_live(entry, moment) and matches(entry["pattern"], plate, bool(watch["lookalikes"]))
               for entry in db.execute("SELECT pattern,expires_at FROM plate_watch_entries WHERE list_id=?", (list_id,)))


def rule_fires(db: sqlite3.Connection, list_id: str, plate: str, now: datetime | None = None) -> bool:
    """Whether a read of this plate raises a rule on the list: a deny list's plate is on it, an allow list's plate is not."""
    on_list = list_matches(db, list_id, plate, now)
    if on_list is None:
        return False
    kind = db.execute("SELECT kind FROM plate_watch_lists WHERE id=?", (list_id,)).fetchone()["kind"]
    return on_list if kind == "deny" else not on_list


# --- search -----------------------------------------------------------------------------------------------------------------------------------

def search(db: sqlite3.Connection, text: str, *, camera_ids: Iterable[str] | None, start: str | None, end: str | None, lookalikes: bool,
           min_confidence: float, limit: int, order: str = "desc") -> list[sqlite3.Row]:
    """Reads matching a full or partial plate (see the module's note on patterns), newest first, on the given cameras (None means every camera)."""
    glob = search_glob(text)
    if glob is None:
        return []
    column, needle = ("folded", fold(glob)) if lookalikes else ("plate", glob)
    clauses, params = [f"r.{column} GLOB ?", "r.confidence>=?"], [needle, min_confidence]
    if camera_ids is not None:
        ids = list(camera_ids)
        if not ids:
            return []
        clauses.append(f"r.camera_id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    if start:
        clauses.append("r.read_at>=?")
        params.append(start)
    if end:
        clauses.append("r.read_at<?")
        params.append(end)
    direction = "ASC" if order == "asc" else "DESC"
    return db.execute(
        "SELECT r.id,r.camera_id,r.track_id,r.plate,r.confidence,r.read_at,r.box_json,r.crop IS NOT NULL AS has_crop,c.name AS camera_name "
        f"FROM plate_reads r JOIN cameras c ON c.id=r.camera_id WHERE {' AND '.join(clauses)} ORDER BY r.read_at {direction},r.id LIMIT ?", (*params, limit)).fetchall()


def read_view(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "camera_id": row["camera_id"], "camera_name": row["camera_name"], "track_id": row["track_id"], "plate": row["plate"],
            "confidence": row["confidence"], "read_at": row["read_at"], "box": json.loads(row["box_json"]), "has_crop": bool(row["has_crop"])}


# --- the worker's side ------------------------------------------------------------------------------------------------------------------------

def overlaps_privacy_zone(box: dict[str, float], zones: Iterable[dict[str, float]]) -> bool:
    """Whether a plate's box touches an area the NVR masks: such a plate is not read, so a masked area never yields a picture or a plate."""
    return any(box["x"] < zone["x"] + zone["width"] and box["x"] + box["width"] > zone["x"] and box["y"] < zone["y"] + zone["height"] and box["y"] + box["height"] > zone["y"]
               for zone in zones)


def keep_plate(camera_id: str, track_id: str, plate: Plate, at: datetime, reader_id: str) -> bool:
    """Store one read in its own transaction (and raise its rules); False when it was not kept because it lies in a privacy mask."""
    from . import privacy
    from .store import connect

    with connect() as db:
        if overlaps_privacy_zone(plate.box, privacy.server_zones(db, camera_id, at)):
            return False
    crop = encode_crop(plate.crop)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            # The track may have gone with its footage while the plate was being read
            if db.execute("SELECT 1 FROM ai_tracks WHERE id=?", (track_id,)).fetchone() is None:
                db.execute("ROLLBACK")
                return False
            store_read(db, camera_id, track_id, plate.text, plate.confidence, at, plate.box, crop, reader_id)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return True


def plate_settings(db: sqlite3.Connection, camera_id: str) -> tuple[bool, float]:
    row = db.execute("SELECT enabled,min_confidence FROM plate_settings WHERE camera_id=?", (camera_id,)).fetchone()
    return (bool(row["enabled"]), float(row["min_confidence"])) if row else (False, 0.6)


class PlateScanner:
    """Gives each vehicle track on one plate camera a few attempts at its plate, never more than one snapshot at a time.

    ``snapshot`` returns a full-size BGR frame of the camera now (or None); ``store`` is called, in a thread, with ``(track_id, Plate)`` for each plate found on a
    tracked vehicle. The scanner knows nothing about databases or streams, so the tests drive it with a stand-in reader and snapshots."""

    def __init__(self, reader: Any, snapshot: Callable[[], Awaitable[np.ndarray | None]], store: Callable[[str, Plate], None], *, min_confidence: float = 0.6,
                 interval: float = ATTEMPT_INTERVAL, max_attempts: int = MAX_ATTEMPTS, clock: Callable[[], float] = time.monotonic) -> None:
        self.reader, self.snapshot, self.store = reader, snapshot, store
        self.min_confidence, self.interval, self.max_attempts, self.clock = min_confidence, interval, max_attempts, clock
        self.attempts: dict[str, int] = {}
        self.done: set[str] = set()
        self.vehicles: dict[str, dict[str, float]] = {}
        self.last = float("-inf")
        self.running = False

    def seen(self, track_id: str, label: str, box: dict[str, float]) -> None:
        if label == "vehicle":
            self.vehicles[track_id] = box

    def due(self) -> bool:
        wanted = any(track_id not in self.done and self.attempts.get(track_id, 0) < self.max_attempts for track_id in self.vehicles)
        return wanted and not self.running and self.clock() - self.last >= self.interval

    async def scan(self) -> int:
        """One attempt, for every vehicle still without a read; returns how many reads were kept."""
        if not self.due():
            return 0
        self.running, self.last = True, self.clock()
        wanted = {track_id: box for track_id, box in self.vehicles.items() if track_id not in self.done and self.attempts.get(track_id, 0) < self.max_attempts}
        for track_id in wanted:
            self.attempts[track_id] = self.attempts.get(track_id, 0) + 1
        kept = 0
        try:
            frame = await self.snapshot()
            if frame is None:
                return 0
            for plate in await asyncio.to_thread(self.reader.read, frame):
                if plate.confidence < self.min_confidence:
                    continue
                track_id = associate(plate.box, wanted.items())
                if track_id is None:
                    continue
                await asyncio.to_thread(self.store, track_id, plate)
                self.done.add(track_id)
                kept += 1
        except Exception:  # noqa: BLE001 - a snapshot or read that fails costs this attempt, never the camera's analysis
            logger.exception("A plate attempt failed")
        finally:
            self.running = False
        return kept

    def forget(self, live: set[str]) -> None:
        """Drop what is kept about tracks that are no longer live."""
        for table in (self.vehicles, self.attempts):
            for track_id in [key for key in table if key not in live]:
                del table[track_id]
        self.done &= live


async def ffmpeg_snapshot(source: str, width: int = SNAPSHOT_WIDTH, height: int = SNAPSHOT_HEIGHT, timeout: float = 12.0) -> np.ndarray | None:
    """One full-size BGR frame of a camera's stream, or None when it could not be had."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    arguments = [ffmpeg, "-v", "error", "-threads", "1"]
    if source.startswith("rtsp"):
        arguments += ["-rtsp_transport", "tcp"]
    arguments += ["-i", source, "-frames:v", "1", "-vf", f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    process = await asyncio.create_subprocess_exec(*arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        data, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return None
    return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3) if len(data) == width * height * 3 else None


# --- accuracy on a labelled sample ------------------------------------------------------------------------------------------------------------

def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        row = [i]
        for j, b in enumerate(right, 1):
            row.append(min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (a != b)))
        previous = row
    return previous[-1]


def score(pairs: Iterable[tuple[str, str | None]]) -> dict[str, Any]:
    """Accuracy over (true plate, read plate or None): whole plates exactly right, characters right (1 - edit distance / length), plates not read at all,
    and plates right once look-alike characters are made one."""
    total = exact = folded = unread = 0
    characters = correct_characters = 0
    for truth, read in pairs:
        truth = normalise(truth)
        total += 1
        characters += len(truth)
        if not read:
            unread += 1
            continue
        read = normalise(read)
        exact += read == truth
        folded += fold(read) == fold(truth)
        correct_characters += max(0, len(truth) - edit_distance(truth, read))
    return {"plates": total, "read": total - unread, "unread": unread, "exact": exact, "exact_percent": round(100 * exact / total, 1) if total else None,
            "lookalike_tolerant": folded, "lookalike_tolerant_percent": round(100 * folded / total, 1) if total else None,
            "character_percent": round(100 * correct_characters / characters, 1) if characters else None}


def evaluate(labels: Path, images: Path, reader: Any) -> dict[str, Any]:
    """Run a reader over a labelled sample: ``labels`` is a CSV of ``image,plate`` rows, ``images`` the folder of PNG/JPEG/BMP images (decoded by ffmpeg).

    Each image's most confident plate read is compared with its label."""
    pairs = []
    with labels.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            frame = _decode_image(images / row["image"])
            reads = reader.read(frame) if frame is not None else []
            best = max(reads, key=lambda plate: plate.confidence, default=None)
            pairs.append((row["plate"], best.text if best else None))
    return score(pairs)


def _decode_image(path: Path) -> np.ndarray | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not path.is_file():
        return None
    probe = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=10, check=False)
    try:
        width, height = (int(item) for item in probe.stdout.strip().split(","))
        data = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"], capture_output=True, timeout=10, check=False).stdout
    except (ValueError, subprocess.TimeoutExpired):
        return None
    return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3) if len(data) == width * height * 3 else None


if __name__ == "__main__":  # python -m app.plates evaluate <labels.csv> <image folder>
    import sys

    if len(sys.argv) != 4 or sys.argv[1] != "evaluate":
        raise SystemExit("usage: python -m app.plates evaluate <labels.csv> <image folder>")
    plate_reader = load_reader()
    if plate_reader is None:
        raise SystemExit("The plate models are not installed (see docs/licence-plates.md)")
    print(json.dumps(evaluate(Path(sys.argv[2]), Path(sys.argv[3]), plate_reader), indent=2))
