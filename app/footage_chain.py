"""Signed footage: per-camera hash chains over recorded segments, signatures and verification.

Each link holds a segment's SHA-256, its time range and size, and the previous link's
hash; its own hash covers all of that. A camera's head is signed with the installation's
Ed25519 key, so a chain cannot be rewritten without the key. Verification recomputes the
chain, checks the latest signature over it, and compares every segment still on disk with
its link. Removals are links too, so retention and deletion are expected, not tampering.

The chain is appended by the background worker, off the recorder's write path.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .credentials import decrypt, encrypt
from .store import connect

GENESIS = "0" * 64
HASH_CHUNK = 1024 * 1024
SIGN_INTERVAL_SECONDS = 60
BATCH = 50
UNREADABLE_RETRY_SECONDS = 600

logger = logging.getLogger(__name__)
# Segments whose file could not be read, with when they were last tried: skipped for a while so they never hold back newer footage.
_unreadable: dict[int, float] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def link_hash(link: dict[str, Any]) -> str:
    """The hash of one link: its fields and the previous link's hash, in a fixed form."""
    canonical = json.dumps({key: link[key] for key in ("camera_id", "seq", "kind", "segment_id", "relative_path", "starts_at", "ends_at",
                                                        "size_bytes", "sha256", "prev_hash")}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def signed_message(camera_id: str, upto_seq: int, head_hash: str) -> bytes:
    return f"nvr-footage-chain:v1:{camera_id}:{upto_seq}:{head_hash}".encode("utf-8")


def _key(db: sqlite3.Connection) -> tuple[Ed25519PrivateKey, str]:
    row = db.execute("SELECT private_key_ciphertext,public_key FROM footage_signing_key WHERE singleton_id=1").fetchone()
    if row is None:
        private = Ed25519PrivateKey.generate()
        raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        public = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        db.execute("INSERT OR IGNORE INTO footage_signing_key(singleton_id,private_key_ciphertext,public_key) VALUES(1,?,?)",
                   (encrypt(base64.b64encode(raw).decode()), public))
        row = db.execute("SELECT private_key_ciphertext,public_key FROM footage_signing_key WHERE singleton_id=1").fetchone()
    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(decrypt(row["private_key_ciphertext"])))
    return private, row["public_key"]


def public_key() -> str:
    with connect() as db:
        return _key(db)[1]


def _head(db: sqlite3.Connection, camera_id: str) -> tuple[int, str]:
    row = db.execute("SELECT seq,link_hash FROM footage_chain WHERE camera_id=? ORDER BY seq DESC LIMIT 1", (camera_id,)).fetchone()
    return (int(row["seq"]), row["link_hash"]) if row else (0, GENESIS)


def _append(db: sqlite3.Connection, link: dict[str, Any]) -> None:
    seq, previous = _head(db, link["camera_id"])
    link = {**link, "seq": seq + 1, "prev_hash": previous}
    link["link_hash"] = link_hash(link)
    db.execute("INSERT INTO footage_chain(camera_id,seq,kind,segment_id,relative_path,starts_at,ends_at,size_bytes,sha256,prev_hash,link_hash,created_at) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (link["camera_id"], link["seq"], link["kind"], link["segment_id"], link["relative_path"], link["starts_at"], link["ends_at"],
                link["size_bytes"], link["sha256"], link["prev_hash"], link["link_hash"], _now()))


def _unchained(db: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    now = time.monotonic()
    for segment_id, tried in list(_unreadable.items()):
        if now - tried >= UNREADABLE_RETRY_SECONDS:
            del _unreadable[segment_id]
    skipped = json.dumps(sorted(_unreadable))
    return db.execute(
        "SELECT s.* FROM recording_segments s, footage_chain_start c WHERE s.id>c.after_segment_id AND s.unavailable_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM footage_chain f WHERE f.kind='segment' AND f.segment_id=s.id AND f.relative_path=s.relative_path) "
        "AND s.id NOT IN (SELECT value FROM json_each(?)) ORDER BY s.id LIMIT ?", (skipped, limit)).fetchall()


async def append_pending() -> int:
    """Hash newly indexed segments and record removals; returns how many were handled, so callers repeat while it is non-zero.

    Hashing reads files outside any transaction."""
    from .storage import recording_file_path

    appended = 0
    with connect() as db:
        pending = _unchained(db, BATCH)
    hashed: list[tuple[sqlite3.Row, str]] = []
    for segment in pending:
        try:
            path = recording_file_path(segment["relative_path"])
            hashed.append((segment, await asyncio.to_thread(file_sha256, path)))
        except (OSError, ValueError):
            _unreadable[segment["id"]] = time.monotonic()  # Missing at hashing time: tried again later, reported as pending until then.
            appended += 1
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for segment, digest in hashed:
                still = db.execute("SELECT 1 FROM recording_segments WHERE id=? AND relative_path=?", (segment["id"], segment["relative_path"])).fetchone()
                if still is None or db.execute("SELECT 1 FROM footage_chain WHERE kind='segment' AND segment_id=? AND relative_path=?",
                                               (segment["id"], segment["relative_path"])).fetchone():
                    continue
                _append(db, {"camera_id": segment["camera_id"], "kind": "segment", "segment_id": segment["id"], "relative_path": segment["relative_path"],
                             "starts_at": segment["starts_at"], "ends_at": segment["ends_at"], "size_bytes": int(segment["size_bytes"]), "sha256": digest})
                appended += 1
            for removal in db.execute("SELECT * FROM footage_removals ORDER BY removed_at, segment_id").fetchall():
                link = db.execute("SELECT * FROM footage_chain f WHERE f.kind='segment' AND f.segment_id=? AND f.camera_id=? AND NOT EXISTS "
                                  "(SELECT 1 FROM footage_chain r WHERE r.kind='removed' AND r.segment_id=f.segment_id AND r.relative_path=f.relative_path AND r.seq>f.seq) "
                                  "ORDER BY f.seq DESC LIMIT 1", (removal["segment_id"], removal["camera_id"])).fetchone()
                if link is not None:
                    _append(db, {"camera_id": link["camera_id"], "kind": "removed", "segment_id": link["segment_id"], "relative_path": link["relative_path"],
                                 "starts_at": link["starts_at"], "ends_at": link["ends_at"], "size_bytes": link["size_bytes"], "sha256": link["sha256"]})
                    appended += 1
                db.execute("DELETE FROM footage_removals WHERE segment_id=?", (removal["segment_id"],))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return appended


def sign_heads() -> int:
    """Sign every camera head that changed since its last signature."""
    signed = 0
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            private, public = _key(db)
            for row in db.execute("SELECT camera_id, MAX(seq) AS seq FROM footage_chain GROUP BY camera_id").fetchall():
                last = db.execute("SELECT upto_seq FROM footage_chain_signatures WHERE camera_id=? ORDER BY upto_seq DESC LIMIT 1", (row["camera_id"],)).fetchone()
                if last is not None and int(last["upto_seq"]) >= int(row["seq"]):
                    continue
                head = db.execute("SELECT link_hash FROM footage_chain WHERE camera_id=? AND seq=?", (row["camera_id"], row["seq"])).fetchone()["link_hash"]
                signature = base64.b64encode(private.sign(signed_message(row["camera_id"], int(row["seq"]), head))).decode()
                db.execute("INSERT INTO footage_chain_signatures(camera_id,upto_seq,head_hash,signature,public_key,signed_at) VALUES(?,?,?,?,?,?)",
                           (row["camera_id"], row["seq"], head, signature, public, _now()))
                signed += 1
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return signed


def verify_signature(public_key_b64: str, camera_id: str, upto_seq: int, head_hash: str, signature_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(base64.b64decode(signature_b64), signed_message(camera_id, upto_seq, head_hash))
        return True
    except (InvalidSignature, ValueError):
        return False


def check_links(links: list[dict[str, Any]], first_prev: str | None = None) -> str | None:
    """None when the links hash correctly in sequence; otherwise what is wrong."""
    previous = first_prev
    expected_seq = links[0]["seq"] if links else 0
    for link in links:
        if link["seq"] != expected_seq:
            return f"link {expected_seq} is missing from the chain"
        if previous is not None and link["prev_hash"] != previous:
            return f"link {link['seq']} does not follow the link before it"
        if link_hash(link) != link["link_hash"]:
            return f"link {link['seq']} was changed after it was written"
        previous = link["link_hash"]
        expected_seq += 1
    return None


def verify(camera_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Check a camera's footage in [start, end) against its signed chain."""
    from .storage import recording_file_path

    with connect() as db:
        links = [dict(row) for row in db.execute("SELECT * FROM footage_chain WHERE camera_id=? ORDER BY seq", (camera_id,))]
        signature = db.execute("SELECT * FROM footage_chain_signatures WHERE camera_id=? ORDER BY upto_seq DESC LIMIT 1", (camera_id,)).fetchone()
        after = db.execute("SELECT after_segment_id FROM footage_chain_start").fetchone()["after_segment_id"]
        segments = [dict(row) for row in db.execute(
            "SELECT * FROM recording_segments WHERE camera_id=? AND starts_at<? AND ends_at>? ORDER BY starts_at, id",
            (camera_id, end.isoformat(), start.isoformat()))]
        _, installation_key = _key(db)
    problems: list[dict[str, Any]] = []
    chain_error = check_links(links, GENESIS)
    signed_upto = 0
    if signature is not None:
        signed_link = next((link for link in links if link["seq"] == signature["upto_seq"]), None)
        if signature["public_key"] != installation_key or signed_link is None or signed_link["link_hash"] != signature["head_hash"] \
                or not verify_signature(signature["public_key"], camera_id, int(signature["upto_seq"]), signature["head_hash"], signature["signature"]):
            chain_error = chain_error or "the chain's signature does not match it"
        else:
            signed_upto = int(signature["upto_seq"])
    removed = {(link["segment_id"], link["relative_path"]) for link in links if link["kind"] == "removed"}
    chained = {(link["segment_id"], link["relative_path"]): link for link in links if link["kind"] == "segment"}
    in_range = [link for link in chained.values() if link["starts_at"] < end.isoformat() and link["ends_at"] > start.isoformat()]
    present = {(segment["id"], segment["relative_path"]): segment for segment in segments}
    results = []
    for segment in segments:
        key = (segment["id"], segment["relative_path"])
        link = chained.get(key)
        entry = {"segment_id": segment["id"], "starts_at": segment["starts_at"], "ends_at": segment["ends_at"]}
        if link is None:
            entry["status"] = "not_signed" if segment["id"] <= after else "pending"
        elif (link["starts_at"], link["ends_at"]) != (segment["starts_at"], segment["ends_at"]):
            entry["status"] = "out_of_order"
        elif segment["unavailable_at"]:
            entry["status"] = "unavailable"
        else:
            try:
                path = recording_file_path(segment["relative_path"])
                entry["status"] = "missing" if not path.is_file() else ("ok" if file_sha256(path) == link["sha256"] else "altered")
            except (OSError, ValueError):
                entry["status"] = "missing"
            if entry["status"] == "ok" and link["seq"] > signed_upto:
                entry["status"] = "ok_unsigned_yet"
        results.append(entry)
    # Links whose segment is gone without a removal record: deleted outside the NVR.
    for link in in_range:
        key = (link["segment_id"], link["relative_path"])
        if key not in present and key not in removed:
            results.append({"segment_id": link["segment_id"], "starts_at": link["starts_at"], "ends_at": link["ends_at"], "status": "missing"})
    results.sort(key=lambda item: (item["starts_at"], item["segment_id"]))
    for entry in results:
        if entry["status"] in {"altered", "missing", "out_of_order"}:
            problems.append(entry)
    return {"camera_id": camera_id, "start": start.isoformat(), "end": end.isoformat(),
            "valid": chain_error is None and not problems, "chain_error": chain_error, "signed_upto": signed_upto,
            "public_key": installation_key, "segments": results, "problems": problems}


def extract(db: sqlite3.Connection, camera_id: str, segment_ids: Iterable[int]) -> dict[str, Any] | None:
    """The chain from the first exported segment's link to the latest signature, for offline verification."""
    wanted = set(segment_ids)
    signature = db.execute("SELECT * FROM footage_chain_signatures WHERE camera_id=? ORDER BY upto_seq DESC LIMIT 1", (camera_id,)).fetchone()
    first = db.execute(f"SELECT MIN(seq) FROM footage_chain WHERE camera_id=? AND kind='segment' AND segment_id IN ({','.join('?' * len(wanted)) or 'NULL'})",
                       (camera_id, *wanted)).fetchone()[0] if wanted else None
    if signature is None or first is None or first > signature["upto_seq"]:
        return None
    links = [dict(row) for row in db.execute("SELECT camera_id,seq,kind,segment_id,relative_path,starts_at,ends_at,size_bytes,sha256,prev_hash,link_hash "
                                             "FROM footage_chain WHERE camera_id=? AND seq BETWEEN ? AND ? ORDER BY seq", (camera_id, first, signature["upto_seq"]))]
    return {"camera_id": camera_id, "links": links, "signature": {key: signature[key] for key in ("upto_seq", "head_hash", "signature", "public_key", "signed_at")}}


def verify_extract(chain: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Offline: the extract hashes correctly up to its signed head, the signature holds for its public key,
    and every exported source segment's SHA-256 matches its link."""
    links = chain.get("links") or []
    signature = chain.get("signature") or {}
    error = check_links(links)
    if not error and (not links or links[-1]["seq"] != signature.get("upto_seq") or links[-1]["link_hash"] != signature.get("head_hash")):
        error = "the chain extract does not end at its signed head"
    if not error and not verify_signature(signature.get("public_key", ""), chain.get("camera_id", ""), int(signature.get("upto_seq", 0)),
                                          signature.get("head_hash", ""), signature.get("signature", "")):
        error = "the chain extract's signature is not valid"
    by_segment = {(link["segment_id"], link["relative_path"]): link for link in links if link["kind"] == "segment"}
    mismatched = [source["segment_id"] for source in sources
                  if (link := by_segment.get((source["segment_id"], source["relative_path"]))) is None or link["sha256"] != source["sha256"]]
    if not error and mismatched:
        error = f"exported source segments {mismatched} do not match the signed chain"
    return {"valid": error is None, "error": error, "public_key": signature.get("public_key")}


class FootageChainManager:
    """Appends new segments and removals to the chains and signs the heads, in the background worker."""

    def __init__(self, poll_seconds: float = 5.0, sign_seconds: float = SIGN_INTERVAL_SECONDS) -> None:
        self.poll_seconds = poll_seconds
        self.sign_seconds = sign_seconds
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="footage-chain")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        last_signed = 0.0
        while True:
            try:
                while await append_pending():
                    pass
                if loop.time() - last_signed >= self.sign_seconds:
                    await asyncio.to_thread(sign_heads)
                    last_signed = loop.time()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - tried again next poll
                logger.exception("Footage chain update failed; retrying")
            await asyncio.sleep(self.poll_seconds)
