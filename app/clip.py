"""Image-text embeddings for natural-language search (#127).

An administrator selects a CLIP-family model by putting its OpenVINO files in one directory (``NVR_AI_CLIP_MODEL_DIR``)::

    image_encoder.xml/.bin   text_encoder.xml/.bin   vocab.json   merges.txt

The image encoder takes a 224x224 RGB crop (CLIP mean and standard deviation) and gives an embedding; the text encoder takes the 77 token
ids of a query. Everything runs in this process: no model, query, picture or embedding is sent anywhere, and nothing here opens a network
connection. A track's embedding is made once, from its first sighting, and kept (quantized) in ``track_image_embeddings`` with the id of
the model, so embeddings from different models are never compared. Without a model, natural-language search ranks by the attributes
the query names instead (see ``semantic``).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from .attributes import MIN_REGION_PIXELS, _crop_image
from .similar import cosine, quantize

MODEL_DIR = Path(os.environ.get("NVR_AI_CLIP_MODEL_DIR", "/opt/nvr/models/clip"))
CONTEXT = 77
START, END = 49406, 49407
SIZE = 224
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
_PATTERN = re.compile(r"<\|startoftext\|>|<\|endoftext\|>|'s|'t|'re|'ve|'m|'ll|'d|[^\W\d_]+|\d|[^\s\w]+", re.IGNORECASE)


@lru_cache(maxsize=1)
def _byte_alphabet() -> dict[int, str]:
    """CLIP's reversible byte to printable character map."""
    keep = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    chars, extra = keep[:], 0
    for byte in range(256):
        if byte not in keep:
            keep.append(byte)
            chars.append(256 + extra)
            extra += 1
    return dict(zip(keep, (chr(code) for code in chars)))


class Tokenizer:
    """CLIP's byte-pair tokenizer, from the model's own ``vocab.json`` and ``merges.txt``."""

    def __init__(self, vocab: dict[str, int], merges: list[tuple[str, str]]) -> None:
        self.vocab = vocab
        self.ranks = {pair: rank for rank, pair in enumerate(merges)}
        self._cache: dict[str, list[str]] = {}

    @classmethod
    def from_files(cls, vocab_path: Path, merges_path: Path) -> "Tokenizer":
        vocab = json.loads(vocab_path.read_text(encoding="utf-8"))
        lines = merges_path.read_text(encoding="utf-8").split("\n")
        merges = [tuple(line.split()) for line in lines[1:] if line and not line.startswith("#") and len(line.split()) == 2]
        return cls(vocab, merges)  # type: ignore[arg-type]

    def _bpe(self, word: str) -> list[str]:
        if word in self._cache:
            return self._cache[word]
        parts = list(word[:-1]) + [word[-1] + "</w>"]
        while len(parts) > 1:
            pairs = {(parts[index], parts[index + 1]) for index in range(len(parts) - 1)}
            best = min(pairs, key=lambda pair: self.ranks.get(pair, float("inf")))
            if best not in self.ranks:
                break
            merged, index = [], 0
            while index < len(parts):
                if index < len(parts) - 1 and (parts[index], parts[index + 1]) == best:
                    merged.append(parts[index] + parts[index + 1])
                    index += 2
                else:
                    merged.append(parts[index])
                    index += 1
            parts = merged
        self._cache[word] = parts
        return parts

    def encode(self, text: str) -> np.ndarray:
        """The ``CONTEXT`` token ids of ``text``: start, the words (cut to fit), end, then zeros."""
        alphabet = _byte_alphabet()
        ids: list[int] = []
        for piece in _PATTERN.findall(" ".join(text.split()).lower()):
            word = "".join(alphabet[byte] for byte in piece.encode("utf-8"))
            ids.extend(self.vocab[part] for part in self._bpe(word) if part in self.vocab)
        ids = [START, *ids[:CONTEXT - 2], END]
        return np.array(ids + [0] * (CONTEXT - len(ids)), dtype=np.int64)


class ClipModel:
    """A text encoder and an image encoder that share one embedding space, run through OpenVINO."""

    def __init__(self, image_encoder: Any, text_encoder: Any, tokenizer: Tokenizer, model_id: str) -> None:
        self.image_encoder, self.text_encoder, self.tokenizer, self.model_id = image_encoder, text_encoder, tokenizer, model_id
        self._lock = threading.Lock()  # One inference request at a time: every camera shares this model from different threads

    @staticmethod
    def _unit(result: Any) -> np.ndarray | None:
        values = [np.asarray(value).reshape(-1) for value in (result.values() if hasattr(result, "values") else [result])]
        vector = values[0].astype(np.float32) if values else np.empty(0, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        return vector / norm if vector.size and norm > 0 else None

    def embed_image(self, frame: bytes | None, box: dict[str, float]) -> np.ndarray | None:
        """The embedding of the object in ``box`` of a raw frame, or None when the box holds too few pixels."""
        image = _crop_image(frame, box, (0.0, 1.0, 0.0, 1.0))
        if image.shape[0] * image.shape[1] < MIN_REGION_PIXELS:
            return None
        rows = np.minimum((np.arange(SIZE) * image.shape[0] / SIZE).astype(int), image.shape[0] - 1)
        columns = np.minimum((np.arange(SIZE) * image.shape[1] / SIZE).astype(int), image.shape[1] - 1)
        rgb = image[rows][:, columns][..., ::-1].astype(np.float32) / 255.0  # The frame is BGR; CLIP wants RGB
        tensor = ((rgb - MEAN) / STD).transpose(2, 0, 1)[None].astype(np.float32)
        with self._lock:
            return self._unit(self.image_encoder(tensor))

    def embed_text(self, text: str) -> np.ndarray | None:
        tokens = self.tokenizer.encode(text)[None]
        with self._lock:
            return self._unit(self.text_encoder(tokens))


def load_clip_model(path: Path = MODEL_DIR, provider: str = "CPU", core: Any = None) -> ClipModel | None:
    """The administrator's CLIP model if its files are all in ``path`` and it loads, else None (search then ranks by attributes)."""
    names = ("image_encoder.xml", "image_encoder.bin", "text_encoder.xml", "text_encoder.bin", "vocab.json", "merges.txt")
    if not all((path / name).is_file() for name in names):
        return None
    try:
        if core is None:
            import openvino as ov
            core = ov.Core()
        image = core.compile_model(core.read_model(str(path / "image_encoder.xml")), provider)
        text = core.compile_model(core.read_model(str(path / "text_encoder.xml")), provider)
        tokenizer = Tokenizer.from_files(path / "vocab.json", path / "merges.txt")
        return ClipModel(image, text, tokenizer, f"clip:{path.name}")
    except Exception:  # noqa: BLE001 - a model that will not load must never stop detection or search
        return None


_shared: dict[str, ClipModel | None] = {}


def shared_model() -> ClipModel | None:
    """The model for this process, loaded once (None when it is not installed). Used to embed queries and, in the worker, pictures."""
    key = str(MODEL_DIR)
    if key not in _shared:
        _shared[key] = load_clip_model(MODEL_DIR)
    return _shared[key]


def store_embedding(db: sqlite3.Connection, track_id: str, model_id: str, vector: np.ndarray) -> None:
    db.execute("INSERT OR REPLACE INTO track_image_embeddings(track_id,model_id,dim,vector,created_at) VALUES(?,?,?,?,?)",
               (track_id, model_id, int(vector.size), quantize(vector), datetime.now(timezone.utc).isoformat()))


def scores(db: sqlite3.Connection, model: ClipModel, query: np.ndarray, track_ids: list[str]) -> dict[str, float]:
    """The cosine of the query with each of these tracks' image embeddings (same model and size only); tracks with none are left out."""
    found: dict[str, float] = {}
    vector = quantize(query)
    for start in range(0, len(track_ids), 500):
        part = track_ids[start:start + 500]
        for row in db.execute(f"SELECT track_id,vector FROM track_image_embeddings WHERE model_id=? AND dim=? AND track_id IN ({','.join('?' * len(part))})",
                              (model.model_id, query.size, *part)):
            found[row["track_id"]] = cosine(vector, bytes(row["vector"]))
    return found
