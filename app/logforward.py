"""Off-host log forwarding: RFC 5424 syslog over TCP or TLS, or OTLP over HTTP (JSON), to a configured endpoint.

``NVR_LOG_FORWARD_URL`` chooses it: ``syslog+tcp://host:514``, ``syslog+tls://host:6514``, ``otlp+http://host:4318`` or
``otlp+https://host:4318`` (an OTLP path may follow; ``/v1/logs`` is the default). ``NVR_LOG_FORWARD_CA`` names a CA
bundle for TLS, ``NVR_LOG_FORWARD_LEVEL`` the lowest level sent (default INFO) and ``NVR_LOG_FORWARD_QUEUE`` how many
records may wait (default 2000).

Forwarding never slows the NVR down: a log call only puts the record on a bounded queue, and when the queue is full or
the endpoint is unreachable the records are dropped and counted. A thread sends them. Every record is scrubbed of
stream URLs, credentials and the addresses of the configured cameras before it leaves the process.
"""
from __future__ import annotations

import http.client
import json
import logging
import queue
import re
import select
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from .version import APP_VERSION

logger = logging.getLogger("nvr.logforward")

SEVERITY = {logging.DEBUG: 5, logging.INFO: 9, logging.WARNING: 13, logging.ERROR: 17, logging.CRITICAL: 21}
SYSLOG_SEVERITY = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3, logging.CRITICAL: 2}
DAEMON_FACILITY = 3
CONNECT_TIMEOUT = 5.0
BATCH = 100
SECRETS_REFRESH_SECONDS = 60
BACKOFF_MAX_SECONDS = 30.0
_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>]+")


@dataclass(frozen=True)
class Target:
    kind: str  # "syslog" or "otlp"
    host: str
    port: int
    tls: bool
    path: str = "/v1/logs"


def parse(url: str) -> Target:
    parts = urlsplit(url)
    kinds = {"syslog+tcp": ("syslog", False, 514), "syslog+tls": ("syslog", True, 6514), "otlp+http": ("otlp", False, 4318), "otlp+https": ("otlp", True, 4318)}
    if parts.scheme not in kinds or not parts.hostname:
        raise ValueError("NVR_LOG_FORWARD_URL must look like syslog+tcp://host:514, syslog+tls://host:6514, otlp+http://host:4318 or otlp+https://host:4318")
    kind, tls, default_port = kinds[parts.scheme]
    return Target(kind, parts.hostname, parts.port or default_port, tls, parts.path if kind == "otlp" and parts.path not in ("", "/") else "/v1/logs")


class Scrubber:
    """Removes what a log line must never carry: URLs, and the addresses and credentials of the configured cameras."""

    def __init__(self) -> None:
        self._exact: re.Pattern[str] | None = None
        self._refreshed = 0.0

    def refresh(self) -> None:
        from .credentials import decrypt
        from .store import connect
        secrets: set[str] = set()

        def take_url(value: str) -> None:
            try:
                parts = urlsplit(value)
                secrets.update(item for item in (parts.hostname, parts.username, parts.password) if item)
                if parts.hostname and parts.port:
                    secrets.add(f"{parts.hostname}:{parts.port}")
            except ValueError:
                pass

        with connect() as db:
            for row in db.execute("SELECT camera_ip,device_service_url,motion_service_url,stream_url_ciphertext,live_substream_url_ciphertext,motion_credentials_ciphertext FROM cameras"):
                if row["camera_ip"]:
                    secrets.add(row["camera_ip"])
                for column in ("device_service_url", "motion_service_url"):
                    if row[column]:
                        take_url(row[column])
                for column in ("stream_url_ciphertext", "live_substream_url_ciphertext"):
                    if row[column]:
                        try:
                            take_url(decrypt(row[column]))
                        except RuntimeError:
                            pass
                if row["motion_credentials_ciphertext"]:
                    try:
                        credentials = json.loads(decrypt(row["motion_credentials_ciphertext"]))
                        secrets.update(str(credentials.get(key)) for key in ("username", "password") if credentials.get(key))
                    except (RuntimeError, ValueError, AttributeError):
                        pass
        # Short strings would mangle ordinary words; the URLs and the addresses are what matter.
        secrets = {secret for secret in secrets if len(secret) >= 4}
        ordered = sorted(secrets, key=len, reverse=True)
        self._exact = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(secret) for secret in ordered) + r")(?![A-Za-z0-9])") if ordered else None
        self._refreshed = time.monotonic()

    def due(self) -> bool:
        return time.monotonic() - self._refreshed >= SECRETS_REFRESH_SECONDS

    def __call__(self, text: str) -> str:
        text = _URL.sub("[url redacted]", text)
        return self._exact.sub("[redacted]", text) if self._exact else text


@dataclass(frozen=True)
class Entry:
    at: float
    level: int
    name: str
    message: str


def syslog_line(entry: Entry, host: str, process: str, pid: int) -> bytes:
    """One RFC 5424 message, with the octet-counting frame RFC 6587 and RFC 5425 use over a stream."""
    priority = DAEMON_FACILITY * 8 + SYSLOG_SEVERITY.get(entry.level, 6)
    stamp = datetime.fromtimestamp(entry.at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    msgid = re.sub(r"[^\x21-\x7e]", "_", entry.name)[:32] or "-"
    body = f"<{priority}>1 {stamp} {host} nvr-{process} {pid} {msgid} - ".encode("ascii", "replace") + b"\xef\xbb\xbf" + entry.message.encode("utf-8", "replace")
    return str(len(body)).encode() + b" " + body


def otlp_body(entries: list[Entry], process: str, pid: int) -> bytes:
    def attribute(key: str, value: str) -> dict[str, Any]:
        return {"key": key, "value": {"stringValue": value}}

    records = [{"timeUnixNano": str(int(entry.at * 1_000_000_000)), "severityNumber": SEVERITY.get(entry.level, 9), "severityText": logging.getLevelName(entry.level),
                "body": {"stringValue": entry.message}, "attributes": [attribute("logger", entry.name)]} for entry in entries]
    return json.dumps({"resourceLogs": [{
        "resource": {"attributes": [attribute("service.name", "nvr"), attribute("service.version", APP_VERSION), attribute("nvr.process", process), attribute("process.pid", str(pid))]},
        "scopeLogs": [{"scope": {"name": "nvr"}, "logRecords": records}],
    }]}, separators=(",", ":")).encode()


class Forwarder:
    """Sends the queued records from its own thread, reconnecting with a capped backoff."""

    def __init__(self, target: Target, process: str, level: int, capacity: int, ca_file: str | None) -> None:
        self.target, self.process, self.level = target, process, level
        self.entries: queue.Queue[Entry] = queue.Queue(maxsize=capacity)
        self.scrub = Scrubber()
        self.sent = self.dropped = self.failures = 0
        self._ca_file = ca_file
        self._socket: socket.socket | ssl.SSLSocket | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="log-forwarder", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def submit(self, entry: Entry) -> None:
        try:
            self.entries.put_nowait(entry)
        except queue.Full:
            self.dropped += 1

    # --- the sending thread ---

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                batch = [self.entries.get(timeout=1.0)]
            except queue.Empty:
                continue
            while len(batch) < BATCH:
                try:
                    batch.append(self.entries.get_nowait())
                except queue.Empty:
                    break
            try:
                if self.scrub.due():
                    self._refresh_scrubber()
                batch = [Entry(entry.at, entry.level, entry.name, self.scrub(entry.message)) for entry in batch]
                self._send(batch)
                self.sent += len(batch)
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001 - nothing the endpoint does may stop the thread
                self.failures += 1
                self.dropped += len(batch)
                self._close()
                logger.debug("Log forwarding to %s:%s failed: %s", self.target.host, self.target.port, type(exc).__name__)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)

    def _refresh_scrubber(self) -> None:
        try:
            self.scrub.refresh()
        except Exception:  # noqa: BLE001 - until it succeeds, the URL pattern alone scrubs
            self.scrub._refreshed = time.monotonic()
            logger.debug("The log scrubber could not read the camera list", exc_info=True)

    def _context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=self._ca_file)

    def _send(self, batch: list[Entry]) -> None:
        if self.target.kind == "syslog":
            # A collector says nothing back: a connection that has become readable has been closed or is not one to trust.
            if self._socket is not None and select.select([self._socket], [], [], 0)[0]:
                self._close()
            try:
                self._send_syslog(batch)
            except OSError:
                if self._socket is None:
                    raise
                self._close()  # It had gone stale since the last batch: once more on a fresh connection.
                self._send_syslog(batch)
            return
        self._send_otlp(batch)

    def _send_syslog(self, batch: list[Entry]) -> None:
        if self._socket is None:
            raw = socket.create_connection((self.target.host, self.target.port), timeout=CONNECT_TIMEOUT)
            self._socket = self._context().wrap_socket(raw, server_hostname=self.target.host) if self.target.tls else raw
        host = socket.gethostname()
        self._socket.sendall(b"".join(syslog_line(entry, host, self.process, _pid()) for entry in batch))

    def _send_otlp(self, batch: list[Entry]) -> None:
        connection = (http.client.HTTPSConnection(self.target.host, self.target.port, timeout=CONNECT_TIMEOUT, context=self._context())
                      if self.target.tls else http.client.HTTPConnection(self.target.host, self.target.port, timeout=CONNECT_TIMEOUT))
        try:
            connection.request("POST", self.target.path, otlp_body(batch, self.process, _pid()), {"Content-Type": "application/json"})
            response = connection.getresponse()
            response.read()
            if not 200 <= response.status < 300:
                raise OSError(f"the collector answered {response.status}")
        finally:
            connection.close()

    def _close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None


def _pid() -> int:
    import os
    return os.getpid()


class ForwardHandler(logging.Handler):
    """Hands each record to the forwarder's queue and returns: it never waits on the network."""

    def __init__(self, forwarder: Forwarder) -> None:
        super().__init__(forwarder.level)
        self.forwarder = forwarder
        self.addFilter(lambda record: not record.name.startswith("nvr.logforward"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + logging.Formatter().formatException(record.exc_info)
            self.forwarder.submit(Entry(record.created, record.levelno, record.name, text))
        except Exception:  # noqa: BLE001 - logging must never raise into the caller
            self.handleError(record)


_forwarder: Forwarder | None = None


def install(process: str) -> Forwarder | None:
    """Attach forwarding to the root logger when ``NVR_LOG_FORWARD_URL`` is set. A bad setting is reported and ignored."""
    global _forwarder
    import os
    url = os.environ.get("NVR_LOG_FORWARD_URL", "").strip()
    if not url or _forwarder is not None:
        return _forwarder
    try:
        target = parse(url)
        level = logging.getLevelName(os.environ.get("NVR_LOG_FORWARD_LEVEL", "INFO").upper())
        capacity = int(os.environ.get("NVR_LOG_FORWARD_QUEUE", "2000"))
        if not isinstance(level, int) or capacity < 1:
            raise ValueError("NVR_LOG_FORWARD_LEVEL must be a level name and NVR_LOG_FORWARD_QUEUE a positive number")
    except ValueError as exc:
        logging.getLogger("nvr.logforward").error("Log forwarding is off: %s", exc)
        return None
    forwarder = Forwarder(target, process, level, capacity, os.environ.get("NVR_LOG_FORWARD_CA") or None)
    forwarder.start()
    logging.getLogger().addHandler(ForwardHandler(forwarder))
    _forwarder = forwarder
    return forwarder


def describe(out: Any) -> None:
    """Forwarding counts for this process, for the metrics endpoint."""
    out.declare("nvr_log_forward_enabled", "gauge", "1 when this process forwards its logs off-host.")
    out.sample("nvr_log_forward_enabled", int(_forwarder is not None))
    if _forwarder is not None:
        out.declare("nvr_log_forward_sent_total", "counter", "Log records sent to the endpoint by this process.")
        out.declare("nvr_log_forward_dropped_total", "counter", "Log records dropped because the queue was full or the endpoint failed.")
        out.sample("nvr_log_forward_sent_total", _forwarder.sent)
        out.sample("nvr_log_forward_dropped_total", _forwarder.dropped)
