from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .recording import recordings_path
from .store import connect

VOLUME_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


def configured_volumes() -> dict[str, str]:
    raw = os.environ.get("NVR_STORAGE_VOLUMES", "")
    values = {"default": ""} if not raw else json.loads(raw)
    if not isinstance(values, dict) or "default" not in values:
        raise ValueError("NVR_STORAGE_VOLUMES must be a JSON object containing a default volume")
    cleaned: dict[str, str] = {}
    for name, relative in values.items():
        if not isinstance(name, str) or not VOLUME_NAME_RE.fullmatch(name) or not isinstance(relative, str):
            raise ValueError("Invalid storage volume name or path")
        path = Path(relative)
        if path.is_absolute() or any(part in {"..", "."} for part in path.parts):
            raise ValueError("Storage volume paths must stay within NVR_RECORDINGS_PATH")
        cleaned[name] = path.as_posix().strip(".")
    return cleaned


def refresh_registry(db=None) -> None:
    volumes = configured_volumes()
    if db is not None:
        for name, relative in volumes.items():
            db.execute("INSERT INTO storage_volumes(id,relative_path) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET relative_path=excluded.relative_path", (name,relative))
        return
    with connect() as conn:
        refresh_registry(conn)


def volume_path(volume_id: str) -> Path:
    volumes = configured_volumes()
    if volume_id not in volumes:
        raise OSError("Storage volume is not configured")
    root = recordings_path()
    path = (root / volumes[volume_id]).resolve()
    if path != root and root not in path.parents:
        raise OSError("Storage volume path escapes the recordings root")
    return path


def camera_recordings_dir(camera_id: str) -> Path:
    with connect() as db:
        row = db.execute("SELECT g.storage_volume_id FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    volume_id = row["storage_volume_id"] if row and row["storage_volume_id"] else "default"
    path = volume_path(volume_id)
    if volume_id == "default" and not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise OSError("Assigned recording volume is missing")
    if volume_id != "default" and not os.path.ismount(path):
        raise OSError("Assigned recording volume is not mounted")
    return path / camera_id


def recording_file_path(relative_path: str) -> Path:
    root=recordings_path()
    candidate=(root/relative_path).resolve()
    if root not in candidate.parents:
        raise OSError("Recorded file path escapes the recordings root")
    volumes=configured_volumes()
    matches=[]
    for name,relative in volumes.items():
        base=(root/relative).resolve()
        if candidate==base or base in candidate.parents:
            matches.append((len(base.parts),name,base))
    if not matches:
        raise OSError("Recorded file does not belong to a configured storage volume")
    _,name,base=max(matches)
    if name!="default" and (not base.is_dir() or not os.path.ismount(base)):
        raise OSError("Recorded file storage volume is unavailable")
    return candidate


def _probe_writable(path: Path) -> tuple[bool, float | None]:
    if not path.is_dir() or not os.access(path, os.W_OK):
        return False, None
    started = time.monotonic()
    try:
        with tempfile.NamedTemporaryFile(prefix=".nvr-write-check-", dir=path, delete=True) as probe:
            probe.write(b"nvr")
            probe.flush()
            os.fsync(probe.fileno())
    except OSError:
        return False, None
    return True, round((time.monotonic() - started) * 1000, 1)


def storage_status() -> dict[str, object]:
    volumes = configured_volumes()
    with connect() as db:
        groups = db.execute("SELECT id,name,storage_volume_id FROM camera_groups").fetchall()
        cameras = db.execute("SELECT c.id,c.name,c.recording_status,c.last_error,g.storage_volume_id FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id").fetchall()
    result=[]
    minimum_free=int(os.environ.get("NVR_STORAGE_PRESSURE_FREE_BYTES","1073741824"))
    for name, relative in volumes.items():
        try:
            path=volume_path(name)
            exists=path.is_dir()
            writable,write_latency=_probe_writable(path) if exists else (False,None)
            usage=shutil.disk_usage(path) if exists else None
            free=usage.free if usage else None
            capacity=usage.total if usage else None
            mount_expected=bool(relative)
            mounted=exists and (os.path.ismount(path) if mount_expected else True)
            state="missing" if not exists or not mounted else "read_only" if not writable else "slow" if write_latency is not None and write_latency>250 else "pressure" if free is not None and free<minimum_free else "healthy"
        except (OSError,ValueError):
            path=recordings_path()/relative
            exists=path.is_dir(); mounted=False; writable=False; write_latency=None; free=None; capacity=None; state="unavailable"
        assigned=[dict(id=camera["id"],name=camera["name"],recording_status=camera["recording_status"],last_error=camera["last_error"]) for camera in cameras if (camera["storage_volume_id"] or "default")==name]
        at_risk=state in {"pressure","missing","read_only","unavailable","slow"} and bool(assigned)
        result.append({"id":name,"relative_path":relative,"exists":exists,"mounted":mounted,"writable":writable,"write_latency_ms":write_latency,"capacity_bytes":capacity,"free_bytes":free,"state":state,"retention_at_risk":at_risk,"cameras":assigned})
    unconfigured={camera["storage_volume_id"] for camera in cameras if camera["storage_volume_id"] and camera["storage_volume_id"] not in volumes}
    for name in sorted(unconfigured):
        assigned=[dict(id=camera["id"],name=camera["name"],recording_status=camera["recording_status"],last_error=camera["last_error"]) for camera in cameras if camera["storage_volume_id"]==name]
        result.append({"id":name,"relative_path":None,"exists":False,"mounted":False,"writable":False,"write_latency_ms":None,"capacity_bytes":None,"free_bytes":None,"state":"unconfigured","retention_at_risk":bool(assigned),"cameras":assigned})
    return {"volumes":result,"minimum_free_bytes":minimum_free}


def assign_group_volume(group_id: str, volume_id: str | None) -> None:
    volumes=configured_volumes()
    if volume_id is not None and volume_id not in volumes:
        raise ValueError("Storage volume is not configured")
    refresh_registry()
    with connect() as db:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?",(group_id,)).fetchone() is None:
            raise LookupError("Camera group not found")
        db.execute("UPDATE camera_groups SET storage_volume_id=? WHERE id=?",(volume_id,group_id))
