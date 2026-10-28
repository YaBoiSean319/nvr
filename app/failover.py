"""Failover recorder (#143): a standby NVR that records in the primary's place while the primary is down, and hands the footage back.

**Roles.** An NVR is ``primary`` (the one that normally records), ``standby`` (waits and watches) or ``off``. They share a secret; every message between them is
signed with it (HMAC-SHA256 over the time, method, path and body digest, valid for a minute).

**Lease.** The primary renews a lease with the standby every few seconds. While the standby hears the renewals it stays quiet: its recording is *held* (a camera
whose recording is held behaves as though recording were disabled, reason ``failover_standby``). After ``silence_seconds`` without a renewal (always longer than the
lease) the standby *takes over*: it increments the epoch, stops holding its recording and records every camera of the configuration it copied from the primary.

**Configuration.** The standby fetches the primary's encrypted backup on a schedule and copies the configuration needed to record (cameras, groups, schedules,
retention, masks) from it. It never copies footage, the footage index or the signing key. It needs the primary's credential key, so a standby is first set up by restoring
a backup of the primary on it once (``python -m app.backup restore``); the passphrase for backups is the same on both.

**Handback.** When the primary comes back (or its renewals get through again) and the standby is recording, the standby stops at once and answers "not yet" until its
recorders have stopped, then grants the lease and offers its footage. A primary never records while it has not been granted the lease after starting (it waits a short
grace for an answer; a standby that cannot be reached at all is assumed down), so both never record at once after a restart. The primary imports the footage
(``source='failover'``), where the footage chain picks it up like any other, and tells the standby, which then recovers.

**Partitions.** A primary that is running but cannot reach the standby keeps recording (a standby that is merely down must not stop it), so during a network partition both
may record the same cameras: the standby takes over, the primary carries on, and the first renewal that gets through ends the standby's takeover and imports its footage.
That overlap is the price of never losing recording to a failure of the standby; it is documented in docs/failover-recorder.md.

**Incidents.** Takeover raises an incident on the standby when it happens and on the primary when it learns of it; both recover when the handback is complete.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from . import backup
from .credentials import decrypt, encrypt, key_path
from .incidents import sync_incident
from .store import connect
from .webhooks import validate_target_url

logger = logging.getLogger("nvr.failover")

ROLES = ("off", "primary", "standby")
SIGNATURE_WINDOW_SECONDS = 60
TIMEOUT = httpx.Timeout(20.0, connect=4.0)
STARTUP_GRACE_SECONDS = 20.0  # How long a starting primary waits for the standby's answer before it assumes the standby is down and records
STOP_WAIT_SECONDS = 30.0  # How long a standby waits for its recorders to stop before it grants the lease anyway
TICK_SECONDS = 2.0
POLL_WHILE_HELD_SECONDS = 3.0
MIN_SECRET_LENGTH = 16
MAX_SEGMENT_SECONDS = 4 * 3600
RECORDING_STATES = ("recording", "buffering", "connecting", "reconnecting", "starting", "unavailable", "failed")
# What is copied from the primary's configuration: tables replaced whole, and the cameras' configuration columns (the rest of a camera row is this NVR's own state).
REPLACED_TABLES = ("camera_group_memberships", "recording_policy_overrides", "retention_policy_overrides", "privacy_masks")
CAMERA_COLUMNS = (
    "name", "onvif_uuid", "group_id", "camera_ip", "camera_brand", "device_service_url", "onvif_profile_token", "live_substream_profile_token", "archived_at",
    "stream_url_ciphertext", "live_substream_url_ciphertext", "audio_supported", "audio_enabled", "enabled", "recording_mode", "recording_enabled", "motion_service_url",
    "motion_credentials_ciphertext", "motion_pre_roll_seconds", "motion_post_roll_seconds", "motion_fallback_enabled", "motion_zones_json", "motion_sensitivity",
    "video_codec", "video_width", "video_height",
)


class PeerError(Exception):
    """A request from the peer that is refused; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- settings and state -------------------------------------------------------------------------------------------------------------------------

def read_settings(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute("SELECT * FROM failover_settings WHERE singleton_id=1").fetchone()
    return {"role": row["role"], "peer_url": row["peer_url"], "has_secret": row["secret_ciphertext"] is not None, "allow_private": bool(row["allow_private"]),
            "silence_seconds": int(row["silence_seconds"]), "lease_seconds": int(row["lease_seconds"]), "sync_seconds": int(row["sync_seconds"]),
            "instance_id": row["instance_id"], "updated_at": row["updated_at"]}


def read_state(db: sqlite3.Connection) -> dict[str, Any]:
    return dict(db.execute("SELECT * FROM failover_state WHERE singleton_id=1").fetchone())


def shared_secret(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT secret_ciphertext FROM failover_settings WHERE singleton_id=1").fetchone()
    return decrypt(row["secret_ciphertext"]) if row and row["secret_ciphertext"] is not None else None


def _set_state(db: sqlite3.Connection, **fields: Any) -> None:
    columns = ",".join(f"{name}=?" for name in fields)
    db.execute(f"UPDATE failover_state SET {columns},updated_at=? WHERE singleton_id=1", (*fields.values(), _stamp()))


def held_reason(db: sqlite3.Connection, now: datetime | None = None) -> str | None:
    """Why this NVR must not record now (a coverage reason code), or None when it may.

    A standby records only while it has taken over; a primary does not record while it waits to hear from the standby after starting, or while the standby holds the
    takeover. Read by the recorder on every pass, from the database, so it holds across processes."""
    try:
        row = db.execute("SELECT s.role,t.takeover_active,t.yield_until FROM failover_settings s,failover_state t WHERE s.singleton_id=1 AND t.singleton_id=1").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None  # A database from before failover
        raise  # Any other failure is not a licence to record: the caller's pass is tried again
    if row is None or row["role"] == "off":
        return None
    if row["role"] == "standby":
        return None if row["takeover_active"] else "failover_standby"
    until = _parse(row["yield_until"])
    return "failover_yield" if until and until > (now or _now()) else None


def prepare_startup() -> None:
    """A primary that starts does not record until it has heard from the standby (or waited ``STARTUP_GRACE_SECONDS`` without hearing). Called before the recorder starts."""
    with connect() as db:
        if read_settings(db)["role"] == "primary":
            _set_state(db, yield_until=_stamp(_now() + timedelta(seconds=STARTUP_GRACE_SECONDS)), peer_state="starting")


# --- signing --------------------------------------------------------------------------------------------------------------------------------------

def _message(timestamp: str, method: str, path: str, body: bytes) -> bytes:
    return f"{timestamp}\n{method.upper()}\n{path}\n{hashlib.sha256(body).hexdigest()}".encode("utf-8")


def sign(secret: str, method: str, path: str, body: bytes = b"", timestamp: int | None = None) -> dict[str, str]:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(secret.encode("utf-8"), _message(stamp, method, path, body), hashlib.sha256).hexdigest()
    return {"X-Failover-Timestamp": stamp, "X-Failover-Signature": f"sha256={digest}"}


def verify(secret: str | None, method: str, path: str, body: bytes, timestamp: str | None, signature: str | None, now: float | None = None) -> bool:
    if not secret or not timestamp or not signature or not signature.startswith("sha256="):
        return False
    try:
        stamped = int(timestamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - stamped) > SIGNATURE_WINDOW_SECONDS:
        return False
    expected = hmac.new(secret.encode("utf-8"), _message(timestamp, method, path, body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


def authenticate(method: str, path: str, body: bytes, timestamp: str | None, signature: str | None) -> None:
    """Refuse a request that is not signed with this NVR's failover secret (the same answer however it is wrong)."""
    with connect() as db:
        secret = shared_secret(db)
    if not verify(secret, method, path, body, timestamp, signature):
        raise PeerError(401, "The request is not signed with this NVR's failover secret")


def check_peer_url(url: str, allow_private: bool) -> str:
    try:
        return validate_target_url(url, allow_private).rstrip("/")
    except ValueError as exc:
        raise ValueError(str(exc).replace("Webhook", "Peer").replace("webhook", "peer")) from None


# --- incidents and episodes -----------------------------------------------------------------------------------------------------------------------

TAKEOVER_KEY = "failover:takeover"


def _takeover_incident(db: sqlite3.Connection, role: str, active: bool, details: dict[str, Any]) -> None:
    summary = ("The primary NVR stopped answering: this standby is recording in its place" if role == "standby"
               else "The standby NVR recorded while this primary was away: its footage is being brought back") if active else \
              ("The primary NVR is back and recording again: the standby has handed back" if role == "standby" else "The standby's footage is back in this NVR and it is recording again")
    sync_incident(db, incident_key=TAKEOVER_KEY, camera_id=None, fault_type="failover", summary=summary, active=active, details={"role": role, **details})


def episodes(db: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute("SELECT * FROM failover_episodes ORDER BY started_at DESC,epoch DESC LIMIT ?", (limit,))]


def annotate_gaps(db: sqlite3.Connection, gaps: list[dict[str, Any]]) -> None:
    """Say, on each gap that overlaps the time before a standby took over, that the standby was waiting for the primary to be silent long enough."""
    found = [row for row in db.execute("SELECT silent_since,started_at,role FROM failover_episodes WHERE silent_since IS NOT NULL ORDER BY started_at DESC LIMIT 20")]
    if not found:
        return
    for gap in gaps:
        for row in found:
            if gap["starts_at"] < row["started_at"] and gap["ends_at"] > row["silent_since"]:
                gap["failover"] = {"message": "The standby NVR waits for the primary to be silent before it takes over recording; footage it recorded after that is in the timeline"}
                break


# --- the standby's side of the conversation -------------------------------------------------------------------------------------------------------

def recorders_idle(db: sqlite3.Connection) -> bool:
    marks = ",".join("?" * len(RECORDING_STATES))
    return db.execute(f"SELECT 1 FROM cameras WHERE enabled=1 AND recording_status IN ({marks}) LIMIT 1", RECORDING_STATES).fetchone() is None


def _segments_of(db: sqlite3.Connection, episode: sqlite3.Row) -> list[sqlite3.Row]:
    """Footage the standby recorded since it took over (a segment that began just before is its own: the recorder starts a segment when it starts)."""
    return db.execute("SELECT id,camera_id,relative_path,starts_at,ends_at,size_bytes FROM recording_segments WHERE source='live' AND unavailable_at IS NULL AND ends_at>? AND starts_at<? "
                      "ORDER BY starts_at,id", (episode["started_at"], episode["ended_at"] or "9999")).fetchall()


def standby_lease(payload: dict[str, Any]) -> dict[str, Any]:
    """The standby's answer to the primary's renewal. Always notes the renewal. While the standby records it stops at once (the primary is back) and says "not yet" until
    its recorders have stopped; then it grants the lease and describes the footage it holds for the primary."""
    now = _now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            settings = read_settings(db)
            if settings["role"] != "standby":
                raise PeerError(409, "This NVR is not a standby")
            state = read_state(db)
            _set_state(db, last_renewed_at=_stamp(now), last_contact_at=_stamp(now))
            lease = settings["lease_seconds"]
            if state["takeover_active"]:
                _set_state(db, takeover_active=0, handback_requested_at=_stamp(now))
                answer = {"granted": False, "state": "stopping", "lease_seconds": lease, "epoch": state["epoch"], "handback": None}
            else:
                episode = db.execute("SELECT * FROM failover_episodes WHERE role='standby' AND state='handback' ORDER BY epoch DESC LIMIT 1").fetchone()
                if episode is None:
                    answer = {"granted": True, "state": "watching", "lease_seconds": lease, "epoch": state["epoch"], "handback": None}
                else:
                    requested = _parse(state["handback_requested_at"]) or now
                    if not recorders_idle(db) and (now - requested).total_seconds() < STOP_WAIT_SECONDS:
                        answer = {"granted": False, "state": "stopping", "lease_seconds": lease, "epoch": state["epoch"], "handback": None}
                    else:
                        segments = _segments_of(db, episode)
                        answer = {"granted": True, "state": "handback", "lease_seconds": lease, "epoch": state["epoch"],
                                  "handback": {"epoch": episode["epoch"], "takeover_started_at": episode["started_at"], "silent_since": episode["silent_since"], "segments": len(segments)}}
            if answer["state"] == "stopping" and state["takeover_active"]:
                db.execute("UPDATE failover_episodes SET state='handback' WHERE role='standby' AND epoch=?", (state["epoch"],))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return answer


def standby_footage(epoch: int) -> list[dict[str, Any]]:
    with connect() as db:
        episode = db.execute("SELECT * FROM failover_episodes WHERE role='standby' AND epoch=?", (epoch,)).fetchone()
        if episode is None:
            raise PeerError(404, "No such takeover")
        return [dict(row) for row in _segments_of(db, episode)]


def standby_footage_file(epoch: int, segment_id: int) -> tuple[Path, str]:
    """The file of one segment recorded during the takeover, and its SHA-256."""
    from .footage_chain import file_sha256
    from .storage import recording_file_path

    with connect() as db:
        episode = db.execute("SELECT * FROM failover_episodes WHERE role='standby' AND epoch=?", (epoch,)).fetchone()
        if episode is None:
            raise PeerError(404, "No such takeover")
        row = next((row for row in _segments_of(db, episode) if row["id"] == segment_id), None)
    if row is None:
        raise PeerError(404, "No such footage in that takeover")
    try:
        path = recording_file_path(row["relative_path"])
    except (OSError, ValueError):
        raise PeerError(404, "The footage is not available") from None
    if not path.is_file():
        raise PeerError(404, "The footage is not available")
    return path, file_sha256(path)


def standby_handback_complete(epoch: int, imported: int, imported_bytes: int) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if read_settings(db)["role"] != "standby":
                raise PeerError(409, "This NVR is not a standby")
            episode = db.execute("SELECT * FROM failover_episodes WHERE role='standby' AND epoch=?", (epoch,)).fetchone()
            if episode is None:
                raise PeerError(404, "No such takeover")
            if episode["state"] == "active":
                raise PeerError(409, "The takeover is still going on: the standby has not been asked to stop")
            if episode["state"] != "done":
                db.execute("UPDATE failover_episodes SET state='done',ended_at=?,segments_imported=?,bytes_imported=? WHERE id=?", (_stamp(), imported, imported_bytes, episode["id"]))
                _takeover_incident(db, "standby", False, {"epoch": epoch, "segments_handed_back": imported})
                _set_state(db, handback_requested_at=None, watching_since=_stamp(), last_error=None)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def take_over(db: sqlite3.Connection, silent_since: datetime | None, now: datetime | None = None) -> int:
    """The standby starts recording in the primary's place, inside the caller's transaction; returns the new epoch."""
    now = now or _now()
    epoch = int(read_state(db)["epoch"]) + 1
    db.execute("INSERT INTO failover_episodes(id,epoch,role,silent_since,started_at,state) VALUES(?,?,'standby',?,?,'active')", (str(uuid.uuid4()), epoch, _stamp(silent_since) if silent_since else None, _stamp(now)))
    _set_state(db, takeover_active=1, takeover_started_at=_stamp(now), epoch=epoch, last_error=None)
    _takeover_incident(db, "standby", True, {"epoch": epoch, "silent_since": _stamp(silent_since) if silent_since else None})
    return epoch


def check_takeover(now: datetime | None = None) -> int | None:
    """Take over if the primary has been silent for ``silence_seconds`` (counted from its last renewal, or from when the standby began watching) and a configuration
    has been copied to record from. Returns the epoch when it did."""
    now = now or _now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            settings, state = read_settings(db), read_state(db)
            if settings["role"] != "standby" or state["takeover_active"] or db.execute("SELECT 1 FROM failover_episodes WHERE role='standby' AND state='handback'").fetchone():
                db.execute("ROLLBACK")
                return None
            if not state["watching_since"]:
                _set_state(db, watching_since=_stamp(now))
                db.execute("COMMIT")
                return None
            since = _parse(state["last_renewed_at"]) or _parse(state["watching_since"])
            if (now - since).total_seconds() < settings["silence_seconds"]:
                db.execute("ROLLBACK")
                return None
            if not state["config_created_at"] and db.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 0:
                _set_state(db, last_error="The primary is silent but this standby has no cameras to record: no configuration has been copied from it yet")
                db.execute("COMMIT")
                return None
            epoch = take_over(db, since, now)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return epoch


# --- copying the primary's configuration -----------------------------------------------------------------------------------------------------------

class SyncError(RuntimeError):
    pass


class MissingFootage(Exception):
    """The standby no longer has a segment it listed (its retention removed it): it is skipped, not waited for."""


def apply_configuration(archive: Path) -> dict[str, Any]:
    """Copy the configuration needed to record from an encrypted backup of the primary into this standby.

    Refused unless the backup is the primary's (its credential key is this standby's own), it is not from a newer version, and it passes its integrity check."""
    secret = backup.passphrase()
    if secret is None:
        raise SyncError("Set NVR_BACKUP_PASSPHRASE to the passphrase the primary's backups use")
    with tempfile.TemporaryDirectory(prefix=".failover-", dir=os.environ.get("NVR_BACKUP_WORK", None)) as work:
        work_dir = Path(work)
        os.chmod(work_dir, 0o700)
        plain = work_dir / "backup.tar"
        try:
            with archive.open("rb") as sealed, plain.open("wb") as target:
                backup.decrypt_stream(sealed, target, secret)
        except backup.BackupError as exc:
            raise SyncError(f"The primary's backup could not be opened: {exc}") from None
        with tarfile.open(plain, "r") as tar:
            if sorted(tar.getnames()) != sorted(("manifest.json", *backup.MEMBERS)):
                raise SyncError("The backup does not contain the expected files")
            manifest = json.loads(tar.extractfile("manifest.json").read())
            for member in backup.MEMBERS:
                with tar.extractfile(member) as stream, (work_dir / member).open("wb") as target:
                    shutil.copyfileobj(stream, target)
        for member in backup.MEMBERS:
            if backup._sha256(work_dir / member) != manifest["files"][member]:
                raise SyncError(f"Checksum mismatch for {member}")
        if not key_path().is_file() or (work_dir / "credentials.key").read_bytes() != key_path().read_bytes():
            raise SyncError("The backup is not from the primary this standby was set up from: its credential key differs. Restore a backup of the primary on this standby once "
                            "(python -m app.backup restore) before turning failover on")
        source = work_dir / "nvr.sqlite3"
        check = sqlite3.connect(source)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SyncError("The primary's backup failed its integrity check")
        finally:
            check.close()
        return _mirror(source, manifest)


def _columns(db: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in db.execute(f"PRAGMA {schema}.table_info({table})")]


def _mirror(source: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    db = connect()
    try:
        local_version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if manifest.get("schema_version") and local_version is not None and int(manifest["schema_version"]) > int(local_version):
            raise SyncError("The primary runs a newer version than this standby: update the standby first")
        db.execute("ATTACH DATABASE ? AS src", (str(source),))
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                counts = _copy(db)
                now = _stamp()
                _set_state(db, last_sync_at=now, last_sync_error=None, config_created_at=manifest.get("created_at"))
                sync_incident(db, incident_key="failover:sync", camera_id=None, fault_type="failover_sync", summary="", active=False)
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        finally:
            db.execute("DETACH DATABASE src")
    finally:
        db.close()
    return counts


def _copy(db: sqlite3.Connection) -> dict[str, int]:
    volumes = {row[0] for row in db.execute("SELECT id FROM storage_volumes")} | {"default"}
    counts: dict[str, int] = {}
    # Groups first (cameras refer to them); a storage volume this standby does not have means its own default.
    wanted = {row["id"] for row in db.execute("SELECT id FROM src.camera_groups")}
    for row in db.execute("SELECT * FROM src.camera_groups").fetchall():
        volume = row["storage_volume_id"] if row["storage_volume_id"] in volumes else None
        db.execute("INSERT INTO camera_groups(id,name,storage_volume_id,created_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,storage_volume_id=excluded.storage_volume_id",
                   (row["id"], row["name"], volume, row["created_at"]))
    for (group_id,) in db.execute("SELECT id FROM camera_groups").fetchall():
        if group_id not in wanted:
            db.execute("DELETE FROM camera_groups WHERE id=?", (group_id,))
    counts["groups"] = len(wanted)
    columns = [name for name in CAMERA_COLUMNS if name in _columns(db, "src", "cameras") and name in _columns(db, "main", "cameras")]
    names = ",".join(columns)
    updates = ",".join(f"{name}=excluded.{name}" for name in columns)
    cameras = {row["id"] for row in db.execute("SELECT id FROM src.cameras")}
    db.execute(f"INSERT INTO cameras(id,{names}) SELECT id,{names} FROM src.cameras WHERE true ON CONFLICT(id) DO UPDATE SET {updates}")
    for (camera_id,) in db.execute("SELECT id FROM cameras").fetchall():
        if camera_id in cameras:
            continue
        # A camera removed at the primary: its footage here is kept, so it is switched off and archived rather than deleted.
        if db.execute("SELECT 1 FROM recording_segments WHERE camera_id=? LIMIT 1", (camera_id,)).fetchone():
            db.execute("UPDATE cameras SET enabled=0,archived_at=COALESCE(archived_at,?) WHERE id=?", (_stamp(), camera_id))
        else:
            db.execute("DELETE FROM cameras WHERE id=?", (camera_id,))
    counts["cameras"] = len(cameras)
    for table in REPLACED_TABLES:
        shared = [name for name in _columns(db, "src", table) if name in _columns(db, "main", table)]
        db.execute(f"DELETE FROM {table}")
        joined = ",".join(shared)
        extra = ""
        if table == "camera_group_memberships":
            extra = " WHERE camera_id IN (SELECT id FROM cameras) AND group_id IN (SELECT id FROM camera_groups)"
        elif table == "privacy_masks":
            extra = " WHERE camera_id IN (SELECT id FROM cameras)"
        db.execute(f"INSERT INTO {table}({joined}) SELECT {joined} FROM src.{table}{extra}")
        counts[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    display = db.execute("SELECT * FROM src.site_display_settings WHERE singleton_id=1").fetchone()
    if display is not None:
        db.execute("INSERT INTO site_display_settings(singleton_id,time_zone,date_format,clock_format,updated_at) VALUES(1,?,?,?,?) ON CONFLICT(singleton_id) DO UPDATE SET "
                   "time_zone=excluded.time_zone,date_format=excluded.date_format,clock_format=excluded.clock_format,updated_at=excluded.updated_at",
                   (display["time_zone"], display["date_format"], display["clock_format"], display["updated_at"]))
    return counts


# --- the primary's side: serving its backup, importing footage ----------------------------------------------------------------------------------------

def backup_for_standby() -> Path:
    """The newest encrypted backup (made now if there is none from the last five minutes), for the standby to copy the configuration from."""
    if backup.disabled_reason():
        raise PeerError(409, backup.disabled_reason() or "Backups are not configured")
    with connect() as db:
        if read_settings(db)["role"] != "primary":
            raise PeerError(409, "This NVR is not a primary")
    directory = backup.destination()
    newest = max(directory.glob(f"*{backup.SUFFIX}"), key=lambda path: path.stat().st_mtime, default=None) if directory.is_dir() else None
    if newest is None or time.time() - newest.stat().st_mtime > 300:
        try:
            result = backup.create_backup("failover")
        except backup.BackupError as exc:
            raise PeerError(409, str(exc)) from None
        newest = directory / str(result["file_name"])
    return newest


def _episode(db: sqlite3.Connection, role: str, epoch: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM failover_episodes WHERE role=? AND epoch=?", (role, epoch)).fetchone()


def learn_of_takeover(handback: dict[str, Any]) -> None:
    """The primary learns the standby recorded while it was away: an episode and an incident, until the footage is back."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if _episode(db, "primary", int(handback["epoch"])) is None:
                db.execute("INSERT INTO failover_episodes(id,epoch,role,silent_since,started_at,state) VALUES(?,?,'primary',?,?,'handback')",
                           (str(uuid.uuid4()), int(handback["epoch"]), handback.get("silent_since"), handback["takeover_started_at"]))
                _takeover_incident(db, "primary", True, {"epoch": int(handback["epoch"]), "takeover_started_at": handback["takeover_started_at"], "segments": handback.get("segments")})
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def store_imported(epoch: int, remote: dict[str, Any], path: Path, digest: str) -> bool:
    """Index one segment recorded by the standby (``source='failover'``) once its file is in place; False when it is not one of this NVR's cameras."""
    from .recording import recordings_path

    relative = path.resolve().relative_to(recordings_path().resolve()).as_posix()
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (remote["camera_id"],)).fetchone() is None:
            return False
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes,source) VALUES(?,?,?,?,?,'failover') ON CONFLICT(relative_path) DO NOTHING",
                       (remote["camera_id"], relative, remote["starts_at"], remote["ends_at"], path.stat().st_size))  # The size of the file that is here, not the one the standby said
            segment = db.execute("SELECT id FROM recording_segments WHERE relative_path=?", (relative,)).fetchone()
            db.execute("INSERT OR IGNORE INTO failover_imports(epoch,remote_segment_id,segment_id,camera_id,sha256) VALUES(?,?,?,?,?)", (epoch, remote["id"], segment["id"], remote["camera_id"], digest))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return True


def finish_handback(epoch: int, imported: int, imported_bytes: int) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            episode = _episode(db, "primary", epoch)
            if episode is not None and episode["state"] != "done":
                db.execute("UPDATE failover_episodes SET state='done',ended_at=?,segments_imported=?,bytes_imported=? WHERE id=?", (_stamp(), imported, imported_bytes, episode["id"]))
                _takeover_incident(db, "primary", False, {"epoch": epoch, "segments_imported": imported})
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


# --- the administrator's and the status views -----------------------------------------------------------------------------------------------------

def status_view(db: sqlite3.Connection) -> dict[str, Any]:
    settings, state = read_settings(db), read_state(db)
    role = settings["role"]
    held = held_reason(db)
    if role == "off":
        headline = "Failover is off"
    elif role == "standby":
        if state["takeover_active"]:
            headline = "Taken over: this standby is recording in the primary's place"
        elif db.execute("SELECT 1 FROM failover_episodes WHERE role='standby' AND state='handback'").fetchone():
            headline = "Handing back to the primary"
        else:
            headline = "Standby: watching the primary"
    else:
        headline = {"ok": "Primary: the standby is watching", "unreachable": "Primary: the standby cannot be reached (recording continues)", "refused": "Primary: the standby refused the signature (the secrets differ)",
                    "starting": "Primary: starting, waiting to hear from the standby", "standby_stopping": "Primary: waiting for the standby to stop recording", "handback": "Primary: bringing back the standby's footage"
                    }.get(str(state["peer_state"]), "Primary")
    return {"role": role, "headline": headline, "lease_until": state["lease_until"], "recording_held": held, "peer_state": state["peer_state"], "last_contact_at": state["last_contact_at"],
            "takeover_active": bool(state["takeover_active"]), "takeover_started_at": state["takeover_started_at"], "epoch": state["epoch"], "last_error": state["last_error"],
            "last_sync_at": state["last_sync_at"], "last_sync_error": state["last_sync_error"], "config_created_at": state["config_created_at"]}


# --- the manager --------------------------------------------------------------------------------------------------------------------------------

class FailoverManager:
    """Runs this NVR's side: the primary renews its lease and brings the standby's footage back; the standby watches, copies the configuration and takes over."""

    def __init__(self, tick_seconds: float = TICK_SECONDS) -> None:
        self.tick_seconds = tick_seconds
        self.task: asyncio.Task[None] | None = None
        self._last_renewal = float("-inf")
        self._last_sync = float("-inf")

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="failover")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed pass is tried again at the next
                logger.exception("A failover pass failed")
            await asyncio.sleep(self.tick_seconds)

    async def tick(self) -> None:
        with connect() as db:
            settings = read_settings(db)
            secret = shared_secret(db)
        if settings["role"] == "off" or not settings["peer_url"] or not secret:
            return
        if settings["role"] == "primary":
            await self._primary(settings, secret)
        else:
            await self._standby(settings, secret)

    # The primary

    def _renewal_due(self, settings: dict[str, Any], held: bool) -> bool:
        interval = POLL_WHILE_HELD_SECONDS if held else max(3.0, settings["lease_seconds"] / 4)
        return time.monotonic() - self._last_renewal >= interval

    async def _primary(self, settings: dict[str, Any], secret: str) -> None:
        with connect() as db:
            held = held_reason(db) is not None
            pending = db.execute("SELECT epoch FROM failover_episodes WHERE role='primary' AND state='handback' ORDER BY epoch LIMIT 1").fetchone()
        if self._renewal_due(settings, held):
            self._last_renewal = time.monotonic()
            await self._renew(settings, secret)
            with connect() as db:
                pending = db.execute("SELECT epoch FROM failover_episodes WHERE role='primary' AND state='handback' ORDER BY epoch LIMIT 1").fetchone()
        if pending is not None and not held_now():
            await self._import(settings, secret, int(pending["epoch"]))

    async def _call(self, settings: dict[str, Any], secret: str, method: str, path: str, payload: dict[str, Any] | None = None, **options: Any) -> httpx.Response:
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
        headers = {**sign(secret, method, path, body), "Content-Type": "application/json"}
        url = check_peer_url(settings["peer_url"], settings["allow_private"]) + path
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            return await client.request(method, url, content=body or None, headers=headers, **options)

    async def _renew(self, settings: dict[str, Any], secret: str) -> None:
        sent = _now()
        try:
            response = await self._call(settings, secret, "POST", "/api/inbound/failover/lease", {"instance_id": settings["instance_id"], "lease_seconds": settings["lease_seconds"]})
        except (httpx.HTTPError, ValueError) as exc:
            await asyncio.to_thread(self._record_peer, "unreachable", f"The standby could not be reached: {type(exc).__name__}")
            return
        if response.status_code in (401, 403):
            await asyncio.to_thread(self._record_peer, "refused", "The standby refused the signature: the failover secrets differ")
            return
        if response.status_code != 200:
            await asyncio.to_thread(self._record_peer, "error", f"The standby answered {response.status_code}")
            return
        try:
            answer = response.json()
        except ValueError:
            await asyncio.to_thread(self._record_peer, "error", "The standby's answer was not understood")
            return
        await asyncio.to_thread(self._apply_answer, settings, sent, answer)

    @staticmethod
    def _record_peer(peer_state: str, error: str | None) -> None:
        with connect() as db:
            _set_state(db, peer_state=peer_state, last_error=error)

    @staticmethod
    def _apply_answer(settings: dict[str, Any], sent: datetime, answer: dict[str, Any]) -> None:
        with connect() as db:
            if answer.get("granted") is True:
                _set_state(db, lease_until=_stamp(sent + timedelta(seconds=settings["lease_seconds"])), yield_until=None, last_contact_at=_stamp(), last_error=None,
                           peer_state="handback" if answer.get("handback") else "ok", epoch=int(answer.get("epoch") or 0))
            else:
                _set_state(db, yield_until=_stamp(_now() + timedelta(seconds=settings["lease_seconds"])), last_contact_at=_stamp(), last_error=None, peer_state="standby_stopping")
        if answer.get("granted") is True and isinstance(answer.get("handback"), dict):
            learn_of_takeover(answer["handback"])

    async def _import(self, settings: dict[str, Any], secret: str, epoch: int) -> None:
        """Bring back the footage the standby recorded: each segment once, checked against the digest the standby gives, then tell it so it can recover."""
        from .storage import camera_recordings_dir

        try:
            listing = await self._call(settings, secret, "GET", f"/api/inbound/failover/footage?epoch={epoch}")
            if listing.status_code != 200:
                return
            remote = listing.json().get("segments", [])
        except (httpx.HTTPError, ValueError):
            return
        with connect() as db:
            done = {row[0] for row in db.execute("SELECT remote_segment_id FROM failover_imports WHERE epoch=?", (epoch,))}
        for raw in remote:
            try:
                started, ended = _parse(raw["starts_at"]), _parse(raw["ends_at"])
                if started is None or ended is None or not 0 < (ended - started).total_seconds() <= MAX_SEGMENT_SECONDS:
                    continue
                # What the standby says is cast and rewritten before it is used: the path is made from these, never from the text it sent.
                item = {"id": int(raw["id"]), "camera_id": str(raw["camera_id"]), "starts_at": started.astimezone(timezone.utc).isoformat(), "ends_at": ended.astimezone(timezone.utc).isoformat(),
                        "size_bytes": int(raw["size_bytes"])}
                if item["id"] in done:
                    continue
                directory = camera_recordings_dir(item["camera_id"]) if await asyncio.to_thread(self._has_camera, item["camera_id"]) else None
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if directory is None:
                continue
            target = directory / f"failover-e{epoch}-{item['id']}-{started:%Y%m%dT%H%M%SZ}.mp4"
            try:
                digest = await self._download(settings, secret, epoch, int(item["id"]), target)
            except MissingFootage:
                logger.warning("The standby no longer has footage %s of takeover %s: skipped", item["id"], epoch)
                await asyncio.to_thread(self._skip, epoch, item)
                continue
            except (httpx.HTTPError, OSError, ValueError):
                return  # Tried again at the next pass
            try:
                await asyncio.to_thread(store_imported, epoch, item, target, digest)
            except (OSError, ValueError):
                target.unlink(missing_ok=True)  # Not indexable here: left out rather than retried for ever
                await asyncio.to_thread(self._skip, epoch, item)
        with connect() as db:
            totals = db.execute("SELECT COUNT(i.segment_id),COALESCE(SUM(s.size_bytes),0) FROM failover_imports i LEFT JOIN recording_segments s ON s.id=i.segment_id WHERE i.epoch=?", (epoch,)).fetchone()
        try:
            confirmed = await self._call(settings, secret, "POST", "/api/inbound/failover/handback-complete", {"epoch": epoch, "imported": totals[0], "bytes": totals[1]})
        except (httpx.HTTPError, ValueError):
            return
        if confirmed.status_code == 200:
            await asyncio.to_thread(finish_handback, epoch, int(totals[0]), int(totals[1]))

    @staticmethod
    def _skip(epoch: int, item: dict[str, Any]) -> None:
        with connect() as db:
            db.execute("INSERT OR IGNORE INTO failover_imports(epoch,remote_segment_id,segment_id,camera_id,sha256) VALUES(?,?,NULL,?,'')", (epoch, item["id"], item["camera_id"]))

    @staticmethod
    def _has_camera(camera_id: str) -> bool:
        with connect() as db:
            return db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is not None

    async def _download(self, settings: dict[str, Any], secret: str, epoch: int, segment_id: int, target: Path) -> str:
        path = f"/api/inbound/failover/footage/{segment_id}?epoch={epoch}"
        headers = sign(secret, "GET", path)
        url = check_peer_url(settings["peer_url"], settings["allow_private"]) + path
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.partial")
        digest = hashlib.sha256()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=4.0), follow_redirects=False) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    if response.status_code == 404:
                        raise MissingFootage
                    if response.status_code != 200:
                        raise httpx.HTTPError(f"The standby answered {response.status_code}")
                    expected = response.headers.get("X-Footage-Sha256", "")
                    with partial.open("wb") as handle:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            digest.update(chunk)
                            await asyncio.to_thread(handle.write, chunk)
            if not expected or digest.hexdigest() != expected:
                raise ValueError("The footage did not arrive intact")
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return digest.hexdigest()

    # The standby

    async def _standby(self, settings: dict[str, Any], secret: str) -> None:
        await asyncio.to_thread(check_takeover)
        with connect() as db:
            state = read_state(db)
        due = bool(state["sync_requested"]) or time.monotonic() - self._last_sync >= settings["sync_seconds"]
        if due and not state["takeover_active"]:
            await asyncio.to_thread(self._clear_sync_request)
            self._last_sync = time.monotonic()
            await self.sync(settings, secret)

    @staticmethod
    def _clear_sync_request() -> None:
        with connect() as db:
            _set_state(db, sync_requested=0)

    async def sync(self, settings: dict[str, Any], secret: str) -> dict[str, Any] | None:
        """Fetch the primary's backup and copy its configuration; a failure is kept and shown, and raises an incident once it has gone on for two intervals."""
        try:
            path = "/api/inbound/failover/backup"
            headers = sign(secret, "GET", path)
            url = check_peer_url(settings["peer_url"], settings["allow_private"]) + path
            with tempfile.TemporaryDirectory(prefix=".failover-fetch-") as directory:
                archive = Path(directory) / "primary.nvrbackup"
                async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=4.0), follow_redirects=False) as client:
                    async with client.stream("GET", url, headers=headers) as response:
                        if response.status_code != 200:
                            detail = (await response.aread()).decode("utf-8", "replace")[:200]
                            raise SyncError(f"The primary answered {response.status_code}: {detail}")
                        with archive.open("wb") as handle:
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                await asyncio.to_thread(handle.write, chunk)
                return await asyncio.to_thread(apply_configuration, archive)
        except (httpx.HTTPError, ValueError, SyncError, OSError, tarfile.TarError, sqlite3.Error) as exc:
            message = str(exc) if isinstance(exc, SyncError) else f"The primary could not be reached for its configuration: {type(exc).__name__}"
            await asyncio.to_thread(self._sync_failed, settings, message)
            return None

    @staticmethod
    def _sync_failed(settings: dict[str, Any], message: str) -> None:
        with connect() as db:
            state = read_state(db)
            _set_state(db, last_sync_error=message)
            last = _parse(state["last_sync_at"]) or _parse(state["watching_since"]) or _now()
            sync_incident(db, incident_key="failover:sync", camera_id=None, fault_type="failover_sync",
                          summary=f"The standby cannot copy the primary's configuration: {message}", active=(_now() - last).total_seconds() > 2 * settings["sync_seconds"], details={"error": message})


def held_now() -> bool:
    with connect() as db:
        return held_reason(db) is not None
