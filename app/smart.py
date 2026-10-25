"""Disk health from SMART, where the container can read it.

For each storage volume the disk behind it is found (from the mount, or from ``NVR_SMART_DEVICES``, a JSON object such as
``{"default": "/dev/sda"}`` that names it when the container cannot work it out) and ``smartctl`` is asked for its report as
JSON. Where that cannot be done, because the disk is not passed through to the container, ``smartctl`` is not installed or
permission is refused, the volume is reported as *not available* with the reason: never as healthy.

A report is judged one of:

- **failing**: the drive's own overall assessment is FAILED, an attribute is failing now, or an NVMe drive reports a critical warning or its life used up;
- **warning**: signs that usually come before failure: an attribute that failed in the past, reallocated, pending or uncorrectable sectors, an NVMe drive
  nearly worn out or low on spare, or media errors;
- **healthy**: none of those.

Either of the first two raises an incident for the volume (``disk_health``), which recovers when a later reading is clean.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .incidents import sync_incident

logger = logging.getLogger("nvr.smart")

SMARTCTL_TIMEOUT_SECONDS = 30
REFRESH_SECONDS = float(os.environ.get("NVR_SMART_INTERVAL_SECONDS", str(6 * 3600)))
# ATA attributes whose raw value, when above zero, is an early sign of a failing surface or head.
RAW_COUNT_ATTRIBUTES = {5: "reallocated sectors", 10: "spin retries", 187: "reported uncorrectable errors", 196: "reallocation events",
                        197: "sectors pending reallocation", 198: "offline uncorrectable sectors"}
NVME_WORN_WARNING, NVME_WORN_FAILING = 90, 100


class Unavailable(Exception):
    """No reading could be had; the message says why, for the person reading the interface."""


@dataclass
class Assessment:
    state: str
    findings: list[str] = field(default_factory=list)
    model: str | None = None
    temperature_c: int | None = None
    power_on_hours: int | None = None


def _mount_device(path: Path) -> int:
    return os.stat(path).st_dev


def _is_block_device(path: Path) -> bool:
    try:
        return stat.S_ISBLK(path.stat().st_mode)
    except OSError:
        return False


def device_for(volume_id: str, path: Path, sysfs: Path = Path("/sys"), dev: Path = Path("/dev"), environment: dict[str, str] | None = None,
               stat_device: Callable[[Path], int] = _mount_device, is_block: Callable[[Path], bool] = _is_block_device) -> str:
    """The whole-disk device behind a volume, or ``Unavailable`` saying why not."""
    configured = {}
    raw = (environment if environment is not None else os.environ).get("NVR_SMART_DEVICES", "")
    if raw:
        try:
            configured = json.loads(raw)
        except ValueError:
            raise Unavailable("NVR_SMART_DEVICES is not valid JSON") from None
    if volume_id in configured:
        device = str(configured[volume_id])
        if not device.startswith("/dev/") or ".." in device:
            raise Unavailable("NVR_SMART_DEVICES must name a device under /dev")
        return device
    try:
        number = stat_device(path)
    except OSError:
        raise Unavailable("The volume's storage cannot be read") from None
    major, minor = os.major(number), os.minor(number)
    node = sysfs / "dev" / "block" / f"{major}:{minor}"
    if major == 0 or not node.exists():
        raise Unavailable("The container cannot see which disk holds this volume (it is an overlay or network filesystem, or the disk is not "
                          "passed through). Name the disk in NVR_SMART_DEVICES.")
    resolved = node.resolve()
    # A partition's directory holds a "partition" file and sits inside its disk's directory.
    name = resolved.parent.name if (resolved / "partition").exists() else resolved.name
    device = dev / name
    if not is_block(device):
        raise Unavailable(f"The disk {name} is not passed through to the container. Add it as a device (see the disk health documentation).")
    return str(device)


def run_smartctl(device: str) -> dict[str, Any]:
    executable = os.environ.get("NVR_SMARTCTL") or shutil.which("smartctl")
    if not executable:
        raise Unavailable("smartctl is not installed in this container")
    try:
        result = subprocess.run([executable, "-j", "-a", device], capture_output=True, timeout=SMARTCTL_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise Unavailable("The disk did not answer in time") from None
    except OSError as exc:
        raise Unavailable(f"smartctl could not be run: {type(exc).__name__}") from None
    try:
        report = json.loads(result.stdout or b"{}")
    except ValueError:
        raise Unavailable("smartctl gave no readable report") from None
    messages = " ".join(str(item.get("string", "")) for item in report.get("smartctl", {}).get("messages", []))
    if "ermission denied" in messages or "Operation not permitted" in messages:
        raise Unavailable("The container may not read this disk's SMART data (it needs the SYS_RAWIO capability; see the disk health documentation)")
    if not any(key in report for key in ("smart_status", "ata_smart_attributes", "nvme_smart_health_information_log")):
        raise Unavailable(messages or "The disk does not report SMART data (virtual disks and some USB bridges do not)")
    return report


def assess(report: dict[str, Any]) -> Assessment:
    """Judge a ``smartctl -j -a`` report."""
    failing: list[str] = []
    warning: list[str] = []
    status = report.get("smart_status", {})
    if status.get("passed") is False:
        failing.append("The drive's own health assessment is FAILED")
    for attribute in report.get("ata_smart_attributes", {}).get("table", []):
        name, when = attribute.get("name", f"attribute {attribute.get('id')}"), attribute.get("when_failed", "")
        if when == "FAILING_NOW":
            failing.append(f"{name} is failing now (value {attribute.get('value')}, threshold {attribute.get('thresh')})")
        elif when == "In_the_past":
            warning.append(f"{name} failed in the past")
        raw = attribute.get("raw", {}).get("value", 0)
        if attribute.get("id") in RAW_COUNT_ATTRIBUTES and isinstance(raw, int) and raw > 0 and when != "FAILING_NOW":
            warning.append(f"{raw} {RAW_COUNT_ATTRIBUTES[attribute['id']]}")
    nvme = report.get("nvme_smart_health_information_log")
    if nvme:
        if nvme.get("critical_warning"):
            failing.append(f"The drive reports a critical warning (flags {nvme['critical_warning']})")
        used = nvme.get("percentage_used")
        if isinstance(used, int) and used >= NVME_WORN_FAILING:
            failing.append(f"{used}% of the drive's rated life is used")
        elif isinstance(used, int) and used >= NVME_WORN_WARNING:
            warning.append(f"{used}% of the drive's rated life is used")
        spare, spare_threshold = nvme.get("available_spare"), nvme.get("available_spare_threshold")
        if isinstance(spare, int) and isinstance(spare_threshold, int) and spare < spare_threshold:
            warning.append(f"Spare capacity ({spare}%) is below the drive's threshold ({spare_threshold}%)")
        if isinstance(nvme.get("media_errors"), int) and nvme["media_errors"] > 0:
            warning.append(f"{nvme['media_errors']} media errors")
    state = "failing" if failing else "warning" if warning else "healthy"
    temperature = report.get("temperature", {}).get("current")
    hours = report.get("power_on_time", {}).get("hours")
    return Assessment(state, failing + warning, report.get("model_name"), temperature if isinstance(temperature, int) else None, hours if isinstance(hours, int) else None)


def read_volume(volume_id: str, path: Path, runner: Callable[[str], dict[str, Any]] = run_smartctl, **discovery: Any) -> tuple[str | None, Assessment | None, str | None]:
    """(device, assessment, reason): an assessment when the disk could be read, else the reason it could not."""
    device = None
    try:
        device = device_for(volume_id, path, **discovery)
        return device, assess(runner(device)), None
    except Unavailable as exc:
        return device, None, str(exc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(db: sqlite3.Connection, volume_id: str, device: str | None, assessment: Assessment | None, reason: str | None) -> None:
    """Store a reading and raise or recover the volume's incident."""
    available = assessment is not None
    state = assessment.state if assessment else "unavailable"
    db.execute(
        "INSERT INTO disk_health(volume_id,device,available,state,reason,model,temperature_c,power_on_hours,findings_json,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(volume_id) DO UPDATE SET device=excluded.device,available=excluded.available,state=excluded.state,reason=excluded.reason,model=excluded.model,"
        "temperature_c=excluded.temperature_c,power_on_hours=excluded.power_on_hours,findings_json=excluded.findings_json,checked_at=excluded.checked_at",
        (volume_id, device, int(available), state, reason, assessment.model if assessment else None, assessment.temperature_c if assessment else None,
         assessment.power_on_hours if assessment else None, json.dumps(assessment.findings if assessment else []), _now()))
    findings = assessment.findings if assessment else []
    sync_incident(db, incident_key=f"disk:{volume_id}", camera_id=None, fault_type="disk_health",
                  summary=f"The disk behind recording storage {volume_id} is {state}" + (f": {findings[0]}" if findings else ""),
                  active=state in ("warning", "failing"), details={"volume_id": volume_id, "disk_state": state, "findings": findings, "model": assessment.model if assessment else None})


def refresh(volumes: dict[str, Path], runner: Callable[[str], dict[str, Any]] = run_smartctl, **discovery: Any) -> dict[str, str]:
    """Read every volume's disk now; returns each volume's resulting state. A volume that was removed from the configuration is forgotten."""
    from .store import connect
    states: dict[str, str] = {}
    readings = {volume_id: read_volume(volume_id, path, runner, **discovery) for volume_id, path in volumes.items()}
    with connect() as db:
        for volume_id, (device, assessment, reason) in readings.items():
            record(db, volume_id, device, assessment, reason)
            states[volume_id] = assessment.state if assessment else "unavailable"
        for row in db.execute("SELECT volume_id FROM disk_health").fetchall():
            if row["volume_id"] not in volumes:
                sync_incident(db, incident_key=f"disk:{row['volume_id']}", camera_id=None, fault_type="disk_health", summary="Recording storage is no longer configured", active=False)
                db.execute("DELETE FROM disk_health WHERE volume_id=?", (row["volume_id"],))
    return states


def due(db: sqlite3.Connection, now: datetime | None = None) -> bool:
    """Whether the readings are older than the refresh interval (or there are none)."""
    row = db.execute("SELECT MIN(checked_at) AS oldest FROM disk_health").fetchone()
    if row["oldest"] is None:
        return True
    return ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(row["oldest"])).total_seconds() >= REFRESH_SECONDS


def view(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [{"volume_id": row["volume_id"], "available": bool(row["available"]), "state": row["state"], "reason": row["reason"], "model": row["model"],
             "temperature_c": row["temperature_c"], "power_on_hours": row["power_on_hours"], "findings": json.loads(row["findings_json"]), "checked_at": row["checked_at"],
             "device": row["device"]} for row in db.execute("SELECT * FROM disk_health ORDER BY volume_id")]
