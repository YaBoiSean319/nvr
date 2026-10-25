"""Camera network hardening checks.

An administrator's report of how exposed the cameras are, from four checks on each camera:

- **default_credentials**: the camera's stored credentials (in its stream address, or the ONVIF login) are a well-known factory default,
  compared with a bundled list. Nothing is tried against the camera: the credentials the NVR already holds are compared in memory with
  the list, and neither they nor the matching entry are logged or stored. The finding says only that a default is in use and where;
- **plain_rtsp**: the stream is plain ``rtsp://`` although the camera answers a TLS handshake on a usual RTSPS port;
- **onvif_http**: the ONVIF device service is ``http://`` although the camera answers a TLS handshake on a usual HTTPS port;
- **outside_network**: an address the camera is configured with (stream, ONVIF, or its recorded IP) is outside the camera networks the
  administrator named. With none named the check is not made, and the report says so.

Each finding carries a fingerprint of its condition, free of secrets. An acknowledgement (with the reason the administrator gave) hides the
finding until the fingerprint changes; a finding that stops applying is removed, so one that returns is shown again.

The checks that ask the camera something (the two TLS probes) are bounded the way the NVR's other camera probes are: a bounded number at
once, a short timeout, and at most one probe of each kind per camera in ``PROBE_GAP`` (and one per address in a run, however many cameras share it). A run inside the gap reuses the last answer. Runs happen
on demand and weekly (``HardeningManager``, in the background worker). An administrator can switch the probes off, and the two protocol checks are then not made.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import sqlite3
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .credentials import decrypt
from .store import connect

logger = logging.getLogger(__name__)

PROBE_GAP = timedelta(seconds=float(os.environ.get("NVR_HARDENING_PROBE_GAP_SECONDS", "3600")))
PROBE_CONCURRENCY = 4
PROBE_TIMEOUT_SECONDS = 3.0
SCHEDULE = timedelta(days=7)
POLL_SECONDS = 60.0
MAX_NETWORKS = 16
# The ports a camera usually offers its secure alternative on: RTSPS (Axis, Hikvision, Dahua, UniFi) and HTTPS.
PORTS: dict[str, tuple[int, ...]] = {"rtsps": (322, 8322, 7441), "onvif_https": (443, 8443)}

# Factory defaults that cameras are sold with and published widely (user, password). The user is compared without regard to case.
DEFAULT_CREDENTIALS = frozenset({
    ("admin", ""), ("admin", "admin"), ("admin", "1234"), ("admin", "12345"), ("admin", "123456"), ("admin", "1111"), ("admin", "111111"),
    ("admin", "4321"), ("admin", "9999"), ("admin", "888888"), ("admin", "666666"), ("admin", "password"), ("admin", "pass"), ("admin", "admin1234"),
    ("admin", "meinsm"), ("admin", "system"), ("admin", "camera"), ("admin", "ipcam"), ("admin", "1234567890"),
    ("root", ""), ("root", "root"), ("root", "pass"), ("root", "admin"), ("root", "12345"), ("root", "1234"), ("root", "vizxv"), ("root", "xc3511"),
    ("root", "xmhdipc"), ("root", "juantech"), ("root", "ikwb"), ("root", "hi3518"), ("root", "klv123"), ("root", "Zte521"),
    ("administrator", ""), ("administrator", "administrator"), ("administrator", "admin"), ("administrator", "1234"),
    ("888888", "888888"), ("666666", "666666"), ("user", "user"), ("guest", "guest"), ("supervisor", "supervisor"), ("service", "service"),
    ("ubnt", "ubnt"), ("default", "default"), ("operator", "operator"), ("viewer", "viewer"),
})

CHECKS: dict[str, dict[str, str]] = {
    "default_credentials": {
        "severity": "high", "title": "Uses a well-known default password",
        "fix": "Set a strong, unique password on the camera itself, then enter the new login for this camera under Cameras (stream address and ONVIF credentials).",
    },
    "plain_rtsp": {
        "severity": "medium", "title": "Streams over plain RTSP although the camera offers RTSPS",
        "fix": "Change the camera's stream address to rtsps:// on the port it offers it on, so the video and the login are not sent in the clear.",
    },
    "onvif_http": {
        "severity": "medium", "title": "Talks ONVIF over HTTP although the camera offers HTTPS",
        "fix": "Change the camera's ONVIF address to https://, so its login and commands are not sent in the clear.",
    },
    "outside_network": {
        "severity": "medium", "title": "Answers on an address outside the camera network",
        "fix": "Move the camera onto the camera network (or add its network under Camera networks if it belongs there), and close any route to it from outside.",
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------------------------------------------- settings

def parse_networks(values: Any) -> list[str]:
    """The camera networks as normalised CIDR strings, or ValueError saying what is wrong."""
    if not isinstance(values, list) or len(values) > MAX_NETWORKS:
        raise ValueError(f"Give up to {MAX_NETWORKS} networks, such as 192.168.50.0/24")
    parsed = []
    for value in values:
        try:
            network = ipaddress.ip_network(str(value).strip(), strict=False)
        except ValueError:
            raise ValueError(f"'{str(value).strip()[:64]}' is not a network address; use a form such as 192.168.50.0/24") from None
        if network.prefixlen == 0:
            raise ValueError("A network that covers every address is not a camera network")
        text = str(network)
        if text not in parsed:
            parsed.append(text)
    return parsed


def networks(db: sqlite3.Connection) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    row = db.execute("SELECT camera_networks_json FROM hardening_settings WHERE id=1").fetchone()
    try:
        return [ipaddress.ip_network(text) for text in json.loads(row["camera_networks_json"])] if row else []
    except (ValueError, TypeError):
        return []


def probes_enabled(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT probes_enabled FROM hardening_settings WHERE id=1").fetchone()
    return bool(row["probes_enabled"]) if row else True


def set_probes_enabled(db: sqlite3.Connection, enabled: bool) -> None:
    db.execute("UPDATE hardening_settings SET probes_enabled=? WHERE id=1", (int(enabled),))


def set_networks(db: sqlite3.Connection, values: list[str]) -> list[str]:
    cleaned = parse_networks(values)
    db.execute("UPDATE hardening_settings SET camera_networks_json=? WHERE id=1", (json.dumps(cleaned),))
    return cleaned


# ------------------------------------------------------------------------------------------------------------------ checks

def _split(url: str | None):
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return None
    return parts if parts.scheme and parts.hostname else None


def _plain(blob: bytes | None) -> str | None:
    if not blob:
        return None
    try:
        return decrypt(blob)
    except Exception:  # noqa: BLE001 - a credential that cannot be read cannot be checked; it is not a reason to stop the run
        return None


def _load_cameras(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every live camera's addresses and logins, in memory for the run only: the logins are never returned, logged or stored."""
    cameras = []
    for row in db.execute("SELECT id,name,camera_ip,device_service_url,motion_service_url,stream_url_ciphertext,live_substream_url_ciphertext,motion_credentials_ciphertext "
                          "FROM cameras WHERE archived_at IS NULL ORDER BY name COLLATE NOCASE,id"):
        onvif = None
        raw = _plain(row["motion_credentials_ciphertext"])
        if raw:
            try:
                loaded = json.loads(raw)
                onvif = loaded if isinstance(loaded, dict) else None
            except ValueError:
                onvif = None
        cameras.append({"id": row["id"], "name": row["name"], "camera_ip": row["camera_ip"], "device_service_url": row["device_service_url"],
                        "motion_service_url": row["motion_service_url"], "stream_url": _plain(row["stream_url_ciphertext"]),
                        "substream_url": _plain(row["live_substream_url_ciphertext"]), "onvif": onvif})
    return cameras


def _default_sources(camera: dict[str, Any]) -> list[str]:
    """Where the camera's stored login is a bundled default: "stream" and/or "onvif". Compared in memory; nothing about the login leaves this function."""
    sources = []
    for url in (camera["stream_url"], camera["substream_url"]):
        parts = _split(url)
        if parts and parts.username is not None and (unquote(parts.username).lower(), unquote(parts.password or "")) in DEFAULT_CREDENTIALS:
            sources.append("stream")
            break
    onvif = camera["onvif"]
    if onvif and str(onvif.get("username") or "") and (str(onvif.get("username")).lower(), str(onvif.get("password") or "")) in DEFAULT_CREDENTIALS:
        sources.append("onvif")
    return sources


def probes_needed(camera: dict[str, Any]) -> dict[str, str]:
    """The probes this camera needs, by kind, with the host to ask: only a plain stream or plain ONVIF address has a secure alternative to look for."""
    needs = {}
    stream = _split(camera["stream_url"])
    if stream and stream.scheme == "rtsp":
        needs["rtsps"] = stream.hostname
    device = _split(camera["device_service_url"])
    if device and device.scheme == "http":
        needs["onvif_https"] = device.hostname
    return needs


def _address(host: str | None):
    try:
        return ipaddress.ip_address((host or "").strip("[]"))
    except ValueError:
        return None


def evaluate(camera: dict[str, Any], camera_networks: list, offered: dict[str, int | None]) -> dict[str, dict[str, str] | None]:
    """Each check's outcome for one camera: None when it does not apply, else the fingerprint and detail of the finding."""
    results: dict[str, dict[str, str] | None] = {name: None for name in CHECKS}
    sources = _default_sources(camera)
    if sources:
        where = " and ".join({"stream": "its stream login", "onvif": "its ONVIF login"}[source] for source in sources)
        results["default_credentials"] = {"fingerprint": "+".join(sources), "detail": f"{camera['name']} uses a well-known factory default for {where}."}
    stream = _split(camera["stream_url"])
    if stream and stream.scheme == "rtsp" and offered.get("rtsps"):
        results["plain_rtsp"] = {"fingerprint": f"{stream.hostname}:{stream.port or 554}>{offered['rtsps']}",
                                 "detail": f"{camera['name']} is read over rtsp://{stream.hostname}:{stream.port or 554} but answers a secure connection on port {offered['rtsps']}."}
    device = _split(camera["device_service_url"])
    if device and device.scheme == "http" and offered.get("onvif_https"):
        results["onvif_http"] = {"fingerprint": f"{device.hostname}:{device.port or 80}>{offered['onvif_https']}",
                                 "detail": f"{camera['name']} is controlled over http://{device.hostname}:{device.port or 80} but answers HTTPS on port {offered['onvif_https']}."}
    if camera_networks:
        hosts = []
        for value in (camera["camera_ip"], (stream.hostname if stream else None), (_split(camera["substream_url"]).hostname if _split(camera["substream_url"]) else None),
                      (device.hostname if device else None), (_split(camera["motion_service_url"]).hostname if _split(camera["motion_service_url"]) else None)):
            address = _address(value)
            if address is not None and not any(address.version == network.version and address in network for network in camera_networks) and str(address) not in hosts:
                hosts.append(str(address))
        if hosts:
            results["outside_network"] = {"fingerprint": ",".join(sorted(hosts)),
                                          "detail": f"{camera['name']} is configured with {', '.join(sorted(hosts))}, outside the camera networks ({', '.join(str(n) for n in camera_networks)})."}
    return results


# ------------------------------------------------------------------------------------------------------------------ probes

def tls_offered(host: str, kind: str) -> int | None:
    """The first usual port for ``kind`` on which ``host`` completes a TLS handshake, else None. Only the handshake is made: nothing is sent after it."""
    context = ssl.create_default_context()
    # The question is whether a secure alternative is offered, not whether its certificate is trusted: cameras present self-signed ones.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    for port in PORTS[kind]:
        try:
            with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_SECONDS) as sock, context.wrap_socket(sock):
                return port
        except (OSError, ssl.SSLError, ValueError):
            continue
    return None


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------------------------------------------- the run

def _sync(db: sqlite3.Connection, camera_id: str, results: dict[str, dict[str, str] | None], now: str) -> None:
    existing = {row["check_name"]: row for row in db.execute("SELECT * FROM hardening_findings WHERE camera_id=?", (camera_id,))}
    for name, result in results.items():
        row = existing.get(name)
        if result is None:
            if row is not None:
                db.execute("DELETE FROM hardening_findings WHERE camera_id=? AND check_name=?", (camera_id, name))
        elif row is None:
            db.execute("INSERT INTO hardening_findings(camera_id,check_name,severity,fingerprint,detail,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?)",
                       (camera_id, name, CHECKS[name]["severity"], result["fingerprint"], result["detail"], now, now))
        elif row["fingerprint"] != result["fingerprint"]:
            # The condition changed: it is a new finding, and an acknowledgement of the old one does not cover it.
            db.execute("UPDATE hardening_findings SET fingerprint=?,detail=?,first_seen_at=?,last_seen_at=?,acknowledged_at=NULL,acknowledged_by=NULL,acknowledged_reason=NULL "
                       "WHERE camera_id=? AND check_name=?", (result["fingerprint"], result["detail"], now, now, camera_id, name))
        else:
            db.execute("UPDATE hardening_findings SET detail=?,last_seen_at=? WHERE camera_id=? AND check_name=?", (result["detail"], now, camera_id, name))


def run(kind: str = "manual", prober: Callable[[str, str], int | None] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Run every check now and return the report. Probes of cameras inside ``PROBE_GAP`` are not repeated: their last answer is used."""
    now = now or _now()
    stamp = now.isoformat()
    prober = prober or tls_offered
    with connect() as db:
        cameras = _load_cameras(db)
        camera_networks = networks(db)
        asking = probes_enabled(db)
        cached = {(row["camera_id"], row["kind"]): row for row in db.execute("SELECT * FROM hardening_probes")}
    offered: dict[str, dict[str, int | None]] = {camera["id"]: {} for camera in cameras}
    # One probe for each host and kind, however many cameras are at that address (a multi-channel encoder is one device).
    pending: dict[tuple[str, str], list[str]] = {}
    for camera in cameras:
        for probe_kind, host in (probes_needed(camera).items() if asking else ()):
            row = cached.get((camera["id"], probe_kind))
            if row is not None and row["host"] == host and timedelta(0) <= now - _parse(row["checked_at"]) < PROBE_GAP:
                offered[camera["id"]][probe_kind] = row["port"]
            else:
                pending.setdefault((host, probe_kind), []).append(camera["id"])

    def probe(item: tuple[str, str]) -> tuple[str, str, int | None, bool]:
        host, probe_kind = item
        try:
            return host, probe_kind, prober(host, probe_kind), True
        except Exception:  # noqa: BLE001 - a probe that failed to run says nothing about the camera; it is asked again next run
            logger.warning("A hardening probe could not be made")
            return host, probe_kind, None, False

    with ThreadPoolExecutor(max_workers=PROBE_CONCURRENCY) as pool:
        probed = list(pool.map(probe, list(pending)))

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for host, probe_kind, port, answered in probed:
                if not answered:
                    continue
                for camera_id in pending[(host, probe_kind)]:
                    offered[camera_id][probe_kind] = port
                    db.execute("INSERT INTO hardening_probes(camera_id,kind,host,checked_at,port) VALUES(?,?,?,?,?) "
                               "ON CONFLICT(camera_id,kind) DO UPDATE SET host=excluded.host,checked_at=excluded.checked_at,port=excluded.port", (camera_id, probe_kind, host, stamp, port))
            live = [camera["id"] for camera in cameras]  # A camera archived since the last run no longer has findings.
            db.execute(f"DELETE FROM hardening_findings WHERE camera_id NOT IN ({','.join('?' * len(live))})", live)
            for camera in cameras:
                _sync(db, camera["id"], evaluate(camera, camera_networks, offered[camera["id"]]), stamp)
            db.execute("UPDATE hardening_settings SET last_run_at=?,last_run_kind=? WHERE id=1", (stamp, kind))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return report(db, now)


def acknowledge(db: sqlite3.Connection, camera_id: str, check: str, reason: str, user_id: int | None, now: datetime | None = None) -> bool:
    """Hide a finding until its condition changes. False when there is no such finding."""
    cursor = db.execute("UPDATE hardening_findings SET acknowledged_at=?,acknowledged_by=?,acknowledged_reason=? WHERE camera_id=? AND check_name=?",
                        ((now or _now()).isoformat(), user_id, reason, camera_id, check))
    return cursor.rowcount > 0


def restore(db: sqlite3.Connection, camera_id: str, check: str) -> bool:
    """Show an acknowledged finding again. False when there is no such acknowledged finding."""
    cursor = db.execute("UPDATE hardening_findings SET acknowledged_at=NULL,acknowledged_by=NULL,acknowledged_reason=NULL WHERE camera_id=? AND check_name=? AND acknowledged_at IS NOT NULL", (camera_id, check))
    return cursor.rowcount > 0


def report(db: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """The findings still showing, those acknowledged, and what the checks cover and when they last ran."""
    now = now or _now()
    settings = db.execute("SELECT * FROM hardening_settings WHERE id=1").fetchone()
    findings, acknowledged = [], []
    for row in db.execute("SELECT f.*,c.name AS camera_name,u.username AS acknowledged_by_name FROM hardening_findings f JOIN cameras c ON c.id=f.camera_id "
                          "LEFT JOIN users u ON u.id=f.acknowledged_by ORDER BY CASE f.severity WHEN 'high' THEN 0 ELSE 1 END,c.name COLLATE NOCASE,f.check_name"):
        meta = CHECKS[row["check_name"]]
        entry = {"camera_id": row["camera_id"], "camera_name": row["camera_name"], "check": row["check_name"], "severity": row["severity"], "title": meta["title"],
                 "detail": row["detail"], "fix": meta["fix"], "first_seen_at": row["first_seen_at"], "last_seen_at": row["last_seen_at"],
                 "acknowledged_at": row["acknowledged_at"], "acknowledged_by": row["acknowledged_by_name"], "reason": row["acknowledged_reason"]}
        (acknowledged if row["acknowledged_at"] else findings).append(entry)
    anchor = settings["last_run_at"] or settings["scheduled_from"]
    configured = networks(db)
    return {
        "as_of": now.isoformat(), "last_run_at": settings["last_run_at"], "last_run_kind": settings["last_run_kind"],
        "next_run_at": (_parse(anchor) + SCHEDULE).isoformat() if anchor else None,
        "camera_networks": [str(network) for network in configured], "network_check": bool(configured), "probes_enabled": bool(settings["probes_enabled"]),
        "probe_gap_seconds": int(PROBE_GAP.total_seconds()), "findings": findings, "acknowledged": acknowledged,
        "counts": {"high": sum(item["severity"] == "high" for item in findings), "medium": sum(item["severity"] == "medium" for item in findings), "acknowledged": len(acknowledged)},
    }


def due(db: sqlite3.Connection, now: datetime | None = None) -> bool:
    """Whether a week has passed since the last run. A fresh installation counts from the first poll, so nothing probes a camera at startup."""
    now = now or _now()
    settings = db.execute("SELECT last_run_at,scheduled_from FROM hardening_settings WHERE id=1").fetchone()
    anchor = settings["last_run_at"] or settings["scheduled_from"]
    if anchor is None:
        db.execute("UPDATE hardening_settings SET scheduled_from=? WHERE id=1", (now.isoformat(),))
        return False
    return now - _parse(anchor) >= SCHEDULE


class HardeningManager:
    """Runs the weekly checks, in the background worker."""

    def __init__(self, poll_seconds: float = POLL_SECONDS) -> None:
        self.poll_seconds = poll_seconds
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="hardening")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    @staticmethod
    def process_once() -> None:
        with connect() as db:
            ready = due(db)
        if ready:
            run("scheduled")

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.process_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - tried again next poll
                logger.exception("The hardening checks failed")
            await asyncio.sleep(self.poll_seconds)
