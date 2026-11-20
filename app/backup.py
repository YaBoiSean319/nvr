"""Encrypted configuration backups and offline restore.

A backup holds the database (inventory, users, policies, views, branding assets,
footage index) and the credential key, so a restore is complete. It never holds
footage. Archives are encrypted with a key derived from ``NVR_BACKUP_PASSPHRASE``
(scrypt) using AES-256-GCM in authenticated chunks; without a passphrase backups
are disabled and nothing is written.

Restore is an offline command run while the NVR is stopped::

    python -m app.backup restore /backups/<file>.nvrbackup

On the next start, index entries whose footage files are missing are marked
unavailable instead of being presented as recorded footage.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
import secrets
import shutil
import sqlite3
import struct
import sys
import tarfile
import tempfile
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import BinaryIO
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .credentials import ensure_key, key_path
from .footage_chain import file_sha256
from .incidents import sync_incident
from .store import connect, database_path
from .version import APP_VERSION
from app.clock import parse as _parse

logger = logging.getLogger("nvr.backup")

MAGIC = b"NVRCFG01"
SUFFIX = ".nvrbackup"
CHUNK_BYTES = 1024 * 1024
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
SCHEDULE_TIME = time(2, 17)
STALE_AFTER = timedelta(hours=26)
MEMBERS = ("nvr.sqlite3", "credentials.key")


class BackupError(RuntimeError):
    pass


# --- configuration ------------------------------------------------------------

def passphrase() -> str | None:
    """The backup passphrase from NVR_BACKUP_PASSPHRASE, or from the file NVR_BACKUP_PASSPHRASE_FILE names."""
    value = os.environ.get("NVR_BACKUP_PASSPHRASE", "")
    if not value and os.environ.get("NVR_BACKUP_PASSPHRASE_FILE"):
        try:
            value = Path(os.environ["NVR_BACKUP_PASSPHRASE_FILE"]).read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
    return value or None


def destination() -> Path:
    return Path(os.environ.get("NVR_BACKUP_PATH", "/backups"))


def keep() -> int:
    try:
        return max(1, min(int(os.environ.get("NVR_BACKUP_KEEP", "14")), 365))
    except ValueError:
        return 14


def disabled_reason() -> str | None:
    if passphrase() is None:
        return "Set NVR_BACKUP_PASSPHRASE (or NVR_BACKUP_PASSPHRASE_FILE) to enable encrypted configuration backups; no unencrypted backup is ever written."
    if len(passphrase() or "") < 16:
        return "NVR_BACKUP_PASSPHRASE must be at least 16 characters."
    return None


# --- encryption -----------------------------------------------------------------

def _derive(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)


class _Sealer:
    """Encrypts what is written to it, a chunk at a time, in the format ``decrypt_stream`` reads: a header, then chunks of CHUNK_BYTES, the
    last one marked final. A backup's archive is written straight into one, so no unencrypted copy of the archive is ever written (#328)."""

    def __init__(self, target: BinaryIO, secret: str) -> None:
        salt, self._prefix = secrets.token_bytes(16), secrets.token_bytes(8)
        self._header = MAGIC + salt + struct.pack(">III", SCRYPT_N, SCRYPT_R, SCRYPT_P) + self._prefix
        self._cipher = AESGCM(_derive(secret, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P))
        self._target = target
        self._buffer = bytearray()
        self._counter = 0
        target.write(self._header)

    def write(self, data: bytes) -> int:
        self._buffer += data
        # A chunk is sealed only once more follows it, so the last one can be marked final when the writing ends.
        while len(self._buffer) > CHUNK_BYTES:
            self._seal(bytes(self._buffer[:CHUNK_BYTES]), final=False)
            del self._buffer[:CHUNK_BYTES]
        return len(data)

    def finish(self) -> None:
        self._seal(bytes(self._buffer), final=True)
        self._buffer.clear()

    def _seal(self, chunk: bytes, final: bool) -> None:
        sealed = self._cipher.encrypt(self._prefix + struct.pack(">I", self._counter), chunk, self._header + struct.pack(">I?", self._counter, final))
        self._target.write(struct.pack(">I", len(sealed)) + sealed)
        self._counter += 1


def encrypt_stream(source: BinaryIO, target: BinaryIO, secret: str) -> None:
    sealer = _Sealer(target, secret)
    shutil.copyfileobj(source, sealer, CHUNK_BYTES)
    sealer.finish()


class _Opener:
    """Reads a backup back as plain bytes, a chunk at a time, checking each chunk as it goes. ``finish`` checks that the backup ended where
    its last chunk says it does: until then, nothing read from it may be trusted to be the whole backup."""

    def __init__(self, source: BinaryIO, secret: str) -> None:
        header = source.read(len(MAGIC) + 16 + 12 + 8)
        if len(header) != len(MAGIC) + 36 or not header.startswith(MAGIC):
            raise BackupError("This is not an NVR configuration backup")
        salt = header[len(MAGIC):len(MAGIC) + 16]
        n, r, p = struct.unpack(">III", header[len(MAGIC) + 16:len(MAGIC) + 28])
        if n > 2**20 or r > 16 or p > 4:
            raise BackupError("Backup key-derivation parameters are out of range")
        self._header, self._prefix = header, header[-8:]
        self._cipher = AESGCM(_derive(secret, salt, n, r, p))
        self._source = source
        self._buffer = bytearray()
        self._counter = 0
        self._ended = False

    def _next_chunk(self) -> None:
        length = self._source.read(4)
        if len(length) != 4:
            raise BackupError("The backup is truncated")
        (size,) = struct.unpack(">I", length)
        if size > CHUNK_BYTES + 16:
            raise BackupError("The backup is corrupt")
        sealed = self._source.read(size)
        if len(sealed) != size:
            raise BackupError("The backup is truncated")
        for final in (False, True):
            try:
                plain = self._cipher.decrypt(self._prefix + struct.pack(">I", self._counter), sealed, self._header + struct.pack(">I?", self._counter, final))
                break
            except InvalidTag:
                plain = None
        if plain is None:
            raise BackupError("Wrong passphrase, or the backup has been altered")
        self._buffer += plain
        self._counter += 1
        if final:
            if self._source.read(1):
                raise BackupError("The backup has unexpected trailing data")
            self._ended = True

    def read(self, size: int = -1) -> bytes:
        while not self._ended and (size < 0 or len(self._buffer) < size):
            self._next_chunk()
        size = len(self._buffer) if size < 0 else min(size, len(self._buffer))
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def finish(self) -> None:
        """Read to the end of the backup, so that a backup cut short or added to is refused."""
        while not self._ended:
            self._next_chunk()
            self._buffer.clear()


def decrypt_stream(source: BinaryIO, target: BinaryIO, secret: str) -> None:
    opener = _Opener(source, secret)
    while chunk := opener.read(CHUNK_BYTES):
        target.write(chunk)
    opener.finish()


# --- creating backups -------------------------------------------------------------

def create_backup(kind: str, *, run_id: str | None = None, database: Path | None = None) -> dict[str, object]:
    """Write one encrypted backup and prune old ones. Raises BackupError on failure."""
    reason = disabled_reason()
    if reason:
        raise BackupError(reason)
    database = database or database_path()
    target_dir = destination()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"Backup destination {target_dir} is not writable") from exc
    if not os.access(target_dir, os.W_OK):
        raise BackupError(f"Backup destination {target_dir} is not writable")
    created = datetime.now(timezone.utc)
    name = f"nvr-config-{created.strftime('%Y%m%dT%H%M%S%fZ')}-{kind.replace('_', '-')}{SUFFIX}"
    final_path = target_dir / name
    partial_path = target_dir / f".{name}.partial"
    # Plaintext only ever exists in the protected data volume, and only as the one snapshot of the database: the archive is encrypted as
    # it is written. A backup needs room for that snapshot where the database is, and for the encrypted archive where backups go (#328).
    _check_space(database, target_dir)
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=database.parent) as work:
        work_dir = Path(work)
        os.chmod(work_dir, 0o700)
        snapshot = work_dir / "nvr.sqlite3"
        source = sqlite3.connect(database)
        copy = sqlite3.connect(snapshot)
        try:
            source.backup(copy)
            try:
                schema_version = copy.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            except sqlite3.Error:
                schema_version = None  # an installation from before versioned migrations
        except sqlite3.Error as exc:
            raise BackupError(f"Could not copy the database for the backup: {exc}") from exc
        finally:
            copy.close()
            source.close()
        try:
            key_file = ensure_key()
        except (OSError, RuntimeError) as exc:
            raise BackupError("The credential key is missing or invalid") from exc
        files = {"nvr.sqlite3": snapshot, "credentials.key": key_file}
        manifest = {
            "format": 1,
            "created_at": created.isoformat(),
            "kind": kind,
            "app_version": APP_VERSION,
            "schema_version": schema_version,
            "files": {member: file_sha256(path) for member, path in files.items()},
            "footage_included": False,
        }
        try:
            with partial_path.open("wb") as sealed:
                os.chmod(partial_path, 0o600)
                sealer = _Sealer(sealed, passphrase() or "")
                with tarfile.open(fileobj=sealer, mode="w|") as tar:  # A stream: each piece is encrypted as it is written
                    data = json.dumps(manifest, indent=2).encode()
                    info = tarfile.TarInfo("manifest.json")
                    info.size, info.mode = len(data), 0o600
                    tar.addfile(info, io.BytesIO(data))
                    for member, path in files.items():
                        tar.add(path, arcname=member, recursive=False, filter=_owner_only)
                sealer.finish()
                sealed.flush()
                os.fsync(sealed.fileno())
            os.replace(partial_path, final_path)
        except BaseException as exc:
            partial_path.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise BackupError(f"Could not write the backup: {exc.strerror or exc}") from exc
            raise
    prune()
    return {"file_name": name, "size_bytes": final_path.stat().st_size, "created_at": created.isoformat(), "run_id": run_id}


SPACE_MARGIN = 64 << 20  # Left free on each disk after a backup, so that making one never fills a disk the NVR is writing to


def _database_bytes(database: Path) -> int:
    """The most the snapshot can take: the database file and its write-ahead log."""
    return sum(item.stat().st_size for item in (database, database.with_name(database.name + "-wal")) if item.exists())


def _check_space(database: Path, target_dir: Path) -> None:
    """Refuse up front, saying how much room is needed where, rather than fail half way with "No space left on device" (#328)."""
    snapshot = _database_bytes(database)
    archive = snapshot + (key_path().stat().st_size if key_path().exists() else 0)
    archive += archive // CHUNK_BYTES * 32 + (64 << 10)  # Encryption adds a few bytes a chunk, the archive a header per file
    staging, destination_folder = database.parent, target_dir
    same_disk = os.stat(staging).st_dev == os.stat(destination_folder).st_dev
    needs = [(staging, snapshot + archive + SPACE_MARGIN)] if same_disk else [(staging, snapshot + SPACE_MARGIN), (destination_folder, archive + SPACE_MARGIN)]
    for folder, needed in needs:
        free = shutil.disk_usage(folder).free
        if free < needed:
            raise BackupError(f"Not enough free space for a backup: it needs about {_megabytes(needed)} free in {folder}, and there is {_megabytes(free)}. "
                              "Free some space on that disk, or send backups to another disk (NVR_BACKUP_PATH). Nothing was written.")


def _megabytes(value: int) -> str:
    return f"{value / (1 << 30):.1f} GB" if value >= 1 << 30 else f"{max(1, value >> 20)} MB"


FAILOVER_KEEP = 2  # A standby's sync makes one of these every few minutes; only the newest are any use


def prune() -> list[str]:
    """Remove backups beyond what is kept. The backups a standby's sync makes are counted on their own: a primary with a standby makes one
    every few minutes, and counted with the rest they pushed every scheduled, manual and pre-migration backup out within hours (#342)."""
    files = sorted(destination().glob(f"nvr-config-*{SUFFIX}"))
    failover = [item for item in files if item.name.endswith(f"-failover{SUFFIX}")]
    others = [item for item in files if item not in failover]
    removed = []
    for stale in others[:-keep()] + failover[:-FAILOVER_KEEP]:
        stale.unlink(missing_ok=True)
        removed.append(stale.name)
    return sorted(removed)


def pre_migration_backup(database: Path) -> None:
    """Migration hook: take a backup before the schema changes, when backups are enabled."""
    if disabled_reason():
        return
    result = create_backup("pre_migration", database=database)
    logger.info("Pre-migration configuration backup written: %s", result["file_name"])


def _owner_only(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o600
    return info


# --- runs, status and schedule ----------------------------------------------------

def queue_backup(actor_id: int) -> dict[str, object]:
    reason = disabled_reason()
    if reason:
        raise BackupError(reason)
    run_id = str(uuid.uuid4())
    with connect() as db:
        db.execute("INSERT INTO backup_runs(id,kind,status,requested_by) VALUES(?,?,?,?)", (run_id, "manual", "queued", actor_id))
        db.execute(
            "INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
            (actor_id, "backup_requested", "backup", run_id, "{}"),
        )
        return run_view(db.execute("SELECT * FROM backup_runs WHERE id=?", (run_id,)).fetchone())


def execute_run(run_id: str) -> dict[str, object]:
    with connect() as db:
        claimed = db.execute(
            "UPDATE backup_runs SET status='running',started_at=? WHERE id=? AND status='queued'",
            (_now(), run_id),
        ).rowcount
    if not claimed:
        raise BackupError("Backup run is not queued")
    try:
        with connect() as db:
            kind = db.execute("SELECT kind FROM backup_runs WHERE id=?", (run_id,)).fetchone()["kind"]
        result = create_backup(kind, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded and raised as an incident
        message = str(exc)[:500] or exc.__class__.__name__
        with connect() as db:
            db.execute("UPDATE backup_runs SET status='failed',finished_at=?,error=? WHERE id=?", (_now(), message, run_id))
        sync_backup_incident()
        raise BackupError(message) from exc
    with connect() as db:
        db.execute(
            "UPDATE backup_runs SET status='succeeded',finished_at=?,file_name=?,size_bytes=? WHERE id=?",
            (_now(), result["file_name"], result["size_bytes"], run_id),
        )
    sync_backup_incident()
    return result


def scheduled_run_due(now: datetime | None = None) -> bool:
    if disabled_reason():
        return False
    now = now or datetime.now(timezone.utc)
    zone = site_zone()
    local = now.astimezone(zone)
    boundary = datetime.combine(local.date(), SCHEDULE_TIME, zone)
    if local < boundary:
        boundary -= timedelta(days=1)
    with connect() as db:
        attempted = db.execute(
            "SELECT 1 FROM backup_runs WHERE kind='scheduled' AND created_at>=? LIMIT 1",
            (_iso(boundary.astimezone(timezone.utc)),),
        ).fetchone()
    return attempted is None


def start_scheduled_run() -> str:
    run_id = str(uuid.uuid4())
    with connect() as db:
        db.execute("INSERT INTO backup_runs(id,kind,status) VALUES(?,?,?)", (run_id, "scheduled", "queued"))
    return run_id


def status() -> dict[str, object]:
    with connect() as db:
        runs = [run_view(row) for row in db.execute("SELECT * FROM backup_runs ORDER BY created_at DESC LIMIT 20")]
        last_success = db.execute("SELECT * FROM backup_runs WHERE status='succeeded' ORDER BY finished_at DESC LIMIT 1").fetchone()
        last_failure = db.execute("SELECT * FROM backup_runs WHERE status='failed' ORDER BY finished_at DESC LIMIT 1").fetchone()
    reason = disabled_reason()
    try:
        files = sorted(destination().glob(f"nvr-config-*{SUFFIX}"))
        stored = [{"file_name": path.name, "size_bytes": path.stat().st_size} for path in files]
        destination_error = None
    except OSError as exc:
        stored, destination_error = [], str(exc)
    age_hours = None
    if last_success:
        age_hours = round((datetime.now(timezone.utc) - _parse(last_success["finished_at"])).total_seconds() / 3600, 2)
    return {
        "enabled": reason is None,
        "disabled_reason": reason,
        "destination": str(destination()),
        "destination_error": destination_error,
        "keep": keep(),
        "schedule": {"local_time": SCHEDULE_TIME.strftime("%H:%M"), "time_zone": str(site_zone())},
        "last_success": run_view(last_success) if last_success else None,
        "last_failure": run_view(last_failure) if last_failure else None,
        "age_hours": age_hours,
        "stale": reason is None and (age_hours is None or age_hours * 3600 > STALE_AFTER.total_seconds()),
        "stored": stored,
        "runs": runs,
        "footage_included": False,
    }


def sync_backup_incident() -> None:
    """One incident while backups are enabled and the latest result failed or is too old."""
    if disabled_reason():
        active, summary = False, "Configuration backups are disabled"
    else:
        with connect() as db:
            latest = db.execute("SELECT * FROM backup_runs WHERE status IN ('succeeded','failed') ORDER BY finished_at DESC LIMIT 1").fetchone()
            first = db.execute("SELECT MIN(created_at) FROM backup_runs").fetchone()[0]
        if latest is not None and latest["status"] == "failed":
            active, summary = True, f"Configuration backup failed: {latest['error']}"
        elif latest is not None and datetime.now(timezone.utc) - _parse(latest["finished_at"]) > STALE_AFTER:
            active, summary = True, "No configuration backup has succeeded in over 26 hours"
        elif latest is None and first is not None and datetime.now(timezone.utc) - _parse(first) > STALE_AFTER:
            active, summary = True, "No configuration backup has succeeded yet"
        else:
            active, summary = False, "Configuration backups are succeeding"
    with connect() as db:
        sync_incident(db, incident_key="backup", camera_id=None, fault_type="backup", summary=summary, active=active)


class BackupManager:
    """Runs scheduled and requested backups in the background worker."""

    def __init__(self, poll_seconds: float = 30.0) -> None:
        self.poll_seconds = poll_seconds
        self.task: asyncio.Task[None] | None = None
        self.wake = asyncio.Event()

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="configuration-backups")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def nudge(self) -> None:
        self.wake.set()

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.process_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Configuration backup cycle failed")
            try:
                await asyncio.wait_for(self.wake.wait(), self.poll_seconds)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    @staticmethod
    def process_once() -> None:
        if scheduled_run_due():
            start_scheduled_run()
        with connect() as db:
            queued = [row[0] for row in db.execute("SELECT id FROM backup_runs WHERE status='queued' ORDER BY created_at")]
        for run_id in queued:
            try:
                execute_run(run_id)
            except BackupError as exc:
                logger.error("Configuration backup failed: %s", exc)
        sync_backup_incident()


def run_view(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in ("id", "kind", "status", "created_at", "started_at", "finished_at", "file_name", "size_bytes", "error")}


def site_zone() -> ZoneInfo:
    try:
        with connect() as db:
            row = db.execute("SELECT time_zone FROM site_display_settings WHERE singleton_id=1").fetchone()
        return ZoneInfo(row["time_zone"] if row else "UTC")
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _now() -> str:
    return _iso(datetime.now(timezone.utc))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- restore ------------------------------------------------------------------------

def restore_marker() -> Path:
    from .workers import control_directory

    return control_directory() / "restore-reconcile.json"


def restore(archive: Path, *, force: bool = False, standby: bool = False) -> dict[str, object]:
    secret = passphrase()
    if secret is None:
        raise BackupError("Set NVR_BACKUP_PASSPHRASE to the passphrase used when the backup was made")
    database = database_path()
    if not force and _installation_running(database):
        raise BackupError("The NVR appears to be running. Stop it before restoring, or pass --force if it is stopped")
    database.parent.mkdir(parents=True, exist_ok=True)
    needed = archive.stat().st_size + SPACE_MARGIN  # The database unpacked from it is no larger than the backup
    free = shutil.disk_usage(database.parent).free
    if free < needed:
        raise BackupError(f"Not enough free space to restore: it needs about {_megabytes(needed)} free in {database.parent}, and there is {_megabytes(free)}. "
                          "Free some space on that disk and try again. Nothing was changed.")
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=database.parent) as work:
        work_dir = Path(work)
        os.chmod(work_dir, 0o700)
        # The backup is decrypted as it is read: the database is the only unencrypted copy written, as when it was made (#328).
        with archive.open("rb") as sealed:
            opener = _Opener(sealed, secret)
            found: dict[str, bytes] = {}
            with tarfile.open(fileobj=opener, mode="r|") as tar:  # A stream: each piece is checked as it is read
                for info in tar:
                    if info.name not in ("manifest.json", *MEMBERS) or info.name in found or not info.isfile():
                        raise BackupError("The backup does not contain the expected files")
                    stream = tar.extractfile(info)
                    if info.name == "manifest.json":
                        found[info.name] = stream.read()
                    else:
                        with (work_dir / info.name).open("wb") as target:
                            shutil.copyfileobj(stream, target)
                        found[info.name] = b""
            opener.finish()
        if sorted(found) != sorted(("manifest.json", *MEMBERS)):
            raise BackupError("The backup does not contain the expected files")
        manifest = json.loads(found["manifest.json"])
        for member in MEMBERS:
            if file_sha256(work_dir / member) != manifest["files"][member]:
                raise BackupError(f"Checksum mismatch for {member}")
        check = sqlite3.connect(work_dir / "nvr.sqlite3")
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("The backed-up database failed its integrity check")
        finally:
            check.close()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        keyfile = key_path()
        preserved = []
        for current in (database, Path(f"{database}-wal"), Path(f"{database}-shm"), keyfile):
            if current.exists():
                aside = current.with_name(f"{current.name}.before-restore-{stamp}")
                os.replace(current, aside)
                preserved.append(str(aside))
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        for member, target in (("nvr.sqlite3", database), ("credentials.key", keyfile)):
            os.replace(work_dir / member, target)
            os.chmod(target, 0o600)
    if standby:
        _make_standby(database)
    marker = restore_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"restored_at": datetime.now(timezone.utc).isoformat(), "backup_created_at": manifest["created_at"]}))
    return {"restored": str(archive), "backup_created_at": manifest["created_at"], "schema_version": manifest["schema_version"], "previous_files": preserved}


def _make_standby(database: Path) -> None:
    """The restored installation is a failover standby: it holds its recording until it takes over, and it knows nobody until its peer and secret are entered.

    Without this a copy of a primary would wake up as a primary and record beside the real one."""
    from .store import initialize

    initialize()  # The backup may predate failover: bring the restored database up to date first
    db = sqlite3.connect(database)
    try:
        db.execute("UPDATE failover_settings SET role='standby',peer_url=NULL,secret_ciphertext=NULL,instance_id=lower(hex(randomblob(16))) WHERE singleton_id=1")
        db.execute("DELETE FROM failover_episodes")
        db.execute("DELETE FROM failover_imports")
        db.execute("UPDATE failover_state SET epoch=0,config_created_at=NULL,lease_until=NULL,yield_until=NULL,peer_state=NULL,last_renewed_at=NULL,handback_requested_at=NULL,watching_since=strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                   "takeover_active=0,takeover_started_at=NULL,last_sync_at=NULL,last_sync_error=NULL,sync_requested=0,last_error=NULL WHERE singleton_id=1")
        db.commit()
    finally:
        db.close()


def reconcile_after_restore() -> dict[str, int] | None:
    """Mark indexed footage whose file is missing as unavailable, once after a restore."""
    marker = restore_marker()
    if not marker.exists():
        return None
    from .recording import recordings_path
    from .storage import segment_available

    if not recordings_path().is_dir():
        # Recordings are not mounted: do not mistake that for lost footage. Try again next start.
        logger.warning("Recordings root %s is unavailable; post-restore reconciliation deferred", recordings_path())
        return None
    marked = checked = skipped_volumes = 0
    stamp = _now()
    with connect() as db:
        rows = db.execute("SELECT id,relative_path FROM recording_segments WHERE unavailable_at IS NULL").fetchall()
        for row in rows:
            checked += 1
            try:
                # Refuses segments on a storage volume that is not mounted.
                present = segment_available(row["relative_path"])
            except (OSError, ValueError):
                skipped_volumes += 1
                continue
            if not present:
                db.execute(
                    "UPDATE recording_segments SET unavailable_at=?,unavailable_reason=? WHERE id=?",
                    (stamp, "Footage file was not found after a configuration restore", row["id"]),
                )
                marked += 1
    if not skipped_volumes:
        marker.unlink(missing_ok=True)
    result = {"checked": checked, "marked_unavailable": marked, "skipped_unavailable_storage": skipped_volumes}
    logger.info("Post-restore footage reconciliation: %s", result)
    return result


def _installation_running(database: Path) -> bool:
    if not database.exists():
        return False
    try:
        db = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        try:
            rows = db.execute("SELECT heartbeat_at FROM worker_status WHERE state='running' AND heartbeat_at IS NOT NULL").fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return False
    now = datetime.now(timezone.utc)
    return any(now - _parse(row[0]) < timedelta(seconds=30) for row in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backup", description="NVR configuration backups")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create", help="write an encrypted configuration backup now")
    restoring = commands.add_parser("restore", help="restore a configuration backup (stop the NVR first)")
    restoring.add_argument("archive", type=Path)
    restoring.add_argument("--force", action="store_true", help="restore even if workers reported liveness recently")
    restoring.add_argument("--standby", action="store_true", help="make the restored NVR a failover standby (it does not record until it takes over; enter its peer and secret afterwards)")
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if arguments.command == "create":
            print(json.dumps(create_backup("manual"), indent=2))
        else:
            print(json.dumps(restore(arguments.archive, force=arguments.force, standby=arguments.standby), indent=2))
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
