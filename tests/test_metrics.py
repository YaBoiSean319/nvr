"""Prometheus metrics and log forwarding: the endpoint and its scope, the series, forwarding over syslog (TCP and TLS) and
OTLP, and that nothing carries a camera address or credential or ever blocks the NVR."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import socket
import ssl
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import logforward, metrics
from app.credentials import encrypt
from app.recording import RecordingManager
from app.store import connect
from tests.test_access_grants import GrantCase

SECRETS = ("192.0.2.50", "192.0.2.51", "192.0.2.52", "hunter2-secret", "cam-user-7", "onvif-pass-9", "rtsp://", "viewer:")
STREAM = "rtsp://viewer:hunter2-secret@192.0.2.50:554/Streaming/Channels/101"


class MetricsCase(GrantCase):
    def setUp(self):
        super().setUp()
        with connect() as db:
            db.execute("UPDATE cameras SET stream_url_ciphertext=?,live_substream_url_ciphertext=?,camera_ip='192.0.2.52',device_service_url='http://192.0.2.51/onvif/device_service',"
                       "motion_credentials_ciphertext=? WHERE id=?",
                       (encrypt(STREAM), encrypt(STREAM.replace("101", "102")), encrypt(json.dumps({"username": "cam-user-7", "password": "onvif-pass-9"})), self.d1))

    def token(self, scopes: list[str]) -> dict[str, str]:
        created = self.admin.post("/api/admin/api-tokens", json={"name": f"scrape-{uuid.uuid4().hex[:6]}", "scopes": scopes})
        self.assertEqual(created.status_code, 201, created.text)
        return {"Authorization": f"Bearer {created.json()['secret']}"}

    def scrape(self) -> str:
        response = TestClient(self.app).get("/metrics", headers=self.token(["metrics"]))
        self.assertEqual(response.status_code, 200, response.text)
        return response.text

    def samples(self, text: str, name: str) -> list[tuple[dict[str, str], float]]:
        found = []
        for line in text.splitlines():
            match = re.fullmatch(rf"{re.escape(name)}(?:\{{(.*)\}})? (\S+)", line)
            if match:
                found.append((dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', match.group(1) or "")), float(match.group(2))))
        return found


class EndpointTests(MetricsCase):
    def test_a_token_with_the_metrics_scope_reads_it_as_prometheus_text(self):
        response = TestClient(self.app).get("/metrics", headers=self.token(["metrics"]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/plain; version=0.0.4"))
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_sessions_and_tokens_without_the_scope_are_refused(self):
        anonymous = TestClient(self.app)
        self.assertEqual(anonymous.get("/metrics").status_code, 401)
        self.assertEqual(self.admin.get("/metrics").status_code, 403)  # An administrator's session is not a scraper.
        self.assertEqual(self.limited.get("/metrics").status_code, 403)
        for scopes in (["read"], ["media", "export"], ["read", "media", "export"]):
            refused = anonymous.get("/metrics", headers=self.token(scopes))
            self.assertEqual(refused.status_code, 403, scopes)
            self.assertIn("'metrics' scope", refused.json()["detail"])

    def test_the_metrics_scope_reads_metrics_and_nothing_else(self):
        headers = self.token(["metrics"])
        client = TestClient(self.app)
        for path in ("/api/cameras", "/api/system/status", "/api/admin/api-tokens", "/api/alarms"):
            self.assertEqual(client.get(path, headers=headers).status_code, 403, path)

    def test_an_admin_token_implies_every_scope_including_this_one(self):
        self.assertEqual(TestClient(self.app).get("/metrics", headers=self.token(["admin"])).status_code, 200)

    def test_the_scope_can_be_granted_and_is_listed(self):
        self.assertIn("metrics", self.admin.get("/api/admin/api-tokens").json()["scopes"])

    def test_metrics_is_not_swallowed_by_the_interface_or_an_api_route(self):
        self.assertTrue(self.scrape().startswith("# HELP"))


class SeriesTests(MetricsCase):
    def test_every_listed_series_is_present(self):
        with connect() as db:
            db.execute("INSERT INTO exports(id,requested_by,camera_ids_json,requested_start,requested_end,status,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?)",
                       ("job1", 1, json.dumps([self.d1]), "2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
            db.execute("INSERT INTO ai_camera_settings(camera_id,enabled,status,last_success_at) VALUES(?,1,'delayed',?)", (self.d1, "2026-01-01T00:00:00Z"))
            metrics.segment_written(db, self.d1, datetime.now(timezone.utc) - timedelta(seconds=3))
            db.execute("UPDATE cameras SET last_footage_write_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), self.d1))
        text = self.scrape()
        for name in ("nvr_build_info", "nvr_camera_recording_state", "nvr_camera_recording_enabled", "nvr_camera_last_footage_write_timestamp_seconds",
                     "nvr_segments_written_total", "nvr_segments_dropped_total", "nvr_segment_write_latency_seconds_bucket", "nvr_segment_write_latency_seconds_sum",
                     "nvr_segment_write_latency_seconds_count", "nvr_live_viewers", "nvr_live_viewer_slots", "nvr_live_audio_listeners", "nvr_live_audio_listener_slots",
                     "nvr_worker_up", "nvr_worker_restarts_total", "nvr_export_jobs", "nvr_export_oldest_queued_age_seconds", "nvr_ai_cameras", "nvr_ai_backlog_cameras",
                     "nvr_ai_seconds_since_last_analysis", "nvr_storage_free_bytes", "nvr_storage_capacity_bytes", "nvr_storage_write_latency_seconds", "nvr_backup_enabled", "nvr_backup_stale", "nvr_log_forward_enabled"):
            self.assertTrue(self.samples(text, name), name)
        for name in ("nvr_camera_recording_state", "nvr_segments_written_total"):
            self.assertEqual({labels["camera_id"] for labels, _ in self.samples(text, name)}, {self.d1, self.d2, self.y1, self.loose})

    def test_the_text_is_well_formed_exposition(self):
        text = self.scrape()
        declared = set()
        for line in text.splitlines():
            if line.startswith("# HELP "):
                continue
            if line.startswith("# TYPE "):
                name, kind = line.split()[2:4]
                self.assertIn(kind, {"gauge", "counter", "histogram"})
                self.assertNotIn(name, declared)
                declared.add(name)
                continue
            self.assertRegex(line, r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{(\w+="([^"\\]|\\.)*",?)*\})? -?[0-9.e+-]+$', line)
            self.assertTrue(any(line.startswith(name) for name in declared), line)
        self.assertTrue(text.endswith("\n"))

    def test_every_family_is_written_whole_with_its_help_and_type_first(self):
        text = self.scrape()
        seen, current = set(), None
        for line in text.splitlines():
            if line.startswith("# HELP "):
                family = line.split()[2]
                self.assertNotIn(family, seen, f"{family} appears twice")
                seen.add(family)
                current = family
            elif line.startswith("# TYPE "):
                self.assertEqual(line.split()[2], current)
            else:
                name = re.match(r"[a-zA-Z_:][a-zA-Z0-9_:]*", line).group(0)
                self.assertEqual(re.sub(r"_(bucket|sum|count)$", "", name) if name.startswith(current) else name, current, line)
        self.assertGreater(len(self.samples(text, "nvr_camera_recording_enabled")), 1)  # Several cameras, still one block each.

    def test_numbers_keep_every_digit(self):
        with connect() as db:
            db.execute("UPDATE cameras SET last_footage_write_at='2026-10-25T13:45:46.500Z' WHERE id=?", (self.d1,))
            db.execute("INSERT INTO metric_counters(name,camera_id,value) VALUES('segments_written',?,1234567)", (self.d1,))
            db.execute("INSERT INTO metric_counters(name,camera_id,value) VALUES('segments_dropped',?,1000001)", (self.d1,))
        text = self.scrape()
        stamp = [value for labels, value in self.samples(text, "nvr_camera_last_footage_write_timestamp_seconds") if labels["camera_id"] == self.d1][0]
        self.assertEqual(stamp, datetime(2026, 10, 25, 13, 45, 46, 500000, tzinfo=timezone.utc).timestamp())
        self.assertIn(f'nvr_segments_written_total{{camera_id="{self.d1}",camera="Dock 1"}} 1234567\n', text + "\n")
        self.assertIn(" 1000001\n", text)
        free = self.samples(text, "nvr_storage_free_bytes")[0][1]
        self.assertEqual(free, int(free))
        self.assertGreater(free, 10 ** 6)
        self.assertNotIn("e+", text)

    def test_a_camera_named_with_its_address_does_not_leak_it_through_a_label(self):
        with connect() as db:
            db.execute("UPDATE cameras SET name=? WHERE id=?", ("Lobby 192.0.2.50 viewer", self.d1))
            db.execute("UPDATE cameras SET name=? WHERE id=?", (STREAM, self.d2))
            db.execute("INSERT INTO ai_camera_settings(camera_id,enabled,status) VALUES(?,1,'analyzed')", (self.d1,))
        text = self.scrape()
        for secret in SECRETS:
            self.assertNotIn(secret, text, secret)
        self.assertIn("Lobby [redacted] [redacted]", text)
        self.assertIn("[url redacted]", text)

    def test_recording_state_is_one_state_per_camera_and_follows_it(self):
        with connect() as db:
            db.execute("UPDATE cameras SET recording_status='recording' WHERE id=?", (self.d1,))
            db.execute("UPDATE cameras SET recording_status='reconnecting' WHERE id=?", (self.d2,))
        states = {labels["camera_id"]: labels["state"] for labels, _ in self.samples(self.scrape(), "nvr_camera_recording_state")}
        self.assertEqual((states[self.d1], states[self.d2]), ("recording", "reconnecting"))

    def test_archived_cameras_are_not_reported(self):
        with connect() as db:
            db.execute("UPDATE cameras SET archived_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), self.loose))
        self.assertNotIn(self.loose, {labels["camera_id"] for labels, _ in self.samples(self.scrape(), "nvr_camera_recording_state")})

    def test_the_latency_histogram_is_cumulative_and_agrees_with_its_count(self):
        now = datetime.now(timezone.utc)
        with connect() as db:
            for seconds in (0.5, 3, 3, 40, 1000):
                metrics.segment_written(db, self.d1, now - timedelta(seconds=seconds))
        text = self.scrape()
        buckets = {labels["le"]: value for labels, value in self.samples(text, "nvr_segment_write_latency_seconds_bucket") if labels["camera_id"] == self.d1}
        self.assertEqual([buckets[le] for le in ("1", "2", "5", "10", "30", "60", "120", "300", "+Inf")], [1, 1, 3, 3, 3, 4, 4, 4, 5])
        count = [value for labels, value in self.samples(text, "nvr_segment_write_latency_seconds_count") if labels["camera_id"] == self.d1]
        total = [value for labels, value in self.samples(text, "nvr_segment_write_latency_seconds_sum") if labels["camera_id"] == self.d1]
        self.assertEqual(count, [5])
        self.assertAlmostEqual(total[0], 0.5 + 3 + 3 + 40 + 1000, delta=2)
        self.assertEqual(list(buckets.values()), sorted(buckets.values()))  # Never decreasing.

    def test_live_viewers_and_slots_follow_the_streams(self):
        from app.api import live_media
        self.assertTrue(live_media._acquire_live_view(self.d1))
        try:
            text = self.scrape()
        finally:
            live_media._release_live_view(self.d1)
        self.assertEqual(self.samples(text, "nvr_live_viewers")[0][1], 1)
        self.assertEqual(self.samples(text, "nvr_live_viewer_slots")[0][1], 16)

    def test_the_export_queue_and_ai_backlog_are_counted(self):
        with connect() as db:
            for index, status in enumerate(("queued", "queued", "running", "failed")):
                db.execute("INSERT INTO exports(id,requested_by,camera_ids_json,requested_start,requested_end,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                           (f"job{index}", 1, "[]", "2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", status, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
            db.execute("INSERT INTO ai_camera_settings(camera_id,enabled,status) VALUES(?,1,'delayed')", (self.d1,))
            db.execute("INSERT INTO ai_camera_settings(camera_id,enabled,status) VALUES(?,1,'analyzed')", (self.d2,))
            db.execute("INSERT INTO ai_camera_settings(camera_id,enabled,status) VALUES(?,0,'failed')", (self.y1,))  # Off: not counted.
        text = self.scrape()
        jobs = {labels["state"]: value for labels, value in self.samples(text, "nvr_export_jobs")}
        self.assertEqual((jobs["queued"], jobs["running"], jobs["failed"], jobs["completed"]), (2, 1, 1, 0))
        self.assertGreater(self.samples(text, "nvr_export_oldest_queued_age_seconds")[0][1], 0)
        self.assertEqual(self.samples(text, "nvr_ai_backlog_cameras")[0][1], 1)

    def test_a_backup_that_is_missing_is_stale_and_storage_reports_free_space(self):
        text = self.scrape()
        self.assertEqual(self.samples(text, "nvr_backup_enabled")[0][1], 0)
        self.assertEqual(self.samples(text, "nvr_backup_stale")[0][1], 0)  # Not configured: nothing to be stale.
        labels, free = self.samples(text, "nvr_storage_free_bytes")[0]
        self.assertEqual(labels, {"volume": "default"})
        self.assertGreater(free, 0)

    def test_labels_are_escaped(self):
        with connect() as db:
            db.execute("UPDATE cameras SET name=? WHERE id=?", ('Gate "North"\nBack\\door', self.d2))
        text = self.scrape()
        self.assertIn('camera="Gate \\"North\\"\\nBack\\\\door"', text)
        self.assertEqual(len(text.splitlines()), len([line for line in text.splitlines() if line]))


class DroppedAndWrittenTests(MetricsCase):
    def index(self, name: str, content: bytes, rows: list[list[str]], manager: RecordingManager) -> int:
        root = Path(self.environment["NVR_RECORDINGS_PATH"])
        output = root / self.d1
        output.mkdir(parents=True, exist_ok=True)
        (output / name).write_bytes(content)
        index = root / "index.csv"
        index.write_text("\n".join(",".join(row) for row in rows))
        return manager._index_csv(self.d1, index, output)

    def counters(self) -> dict[str, float]:
        with connect() as db:
            return {row["name"]: row["value"] for row in db.execute("SELECT name,value FROM metric_counters WHERE camera_id=?", (self.d1,))}

    def test_a_segment_the_recorder_could_not_keep_is_dropped_once_however_often_the_index_is_read(self):
        manager = RecordingManager()
        rows = [["segment-20260101T000000Z.mp4", "0", "10"]]
        for _ in range(3):
            self.assertEqual(self.index("segment-20260101T000000Z.mp4", b"tiny", rows, manager), 0)
        self.assertEqual(self.counters()["segments_dropped"], 1)
        self.assertEqual(self.index("segment-20260101T000010Z.mp4", b"x" * 500, [["segment-20260101T000010Z.mp4", "0", "10"]], manager), 1)
        counters = self.counters()
        self.assertEqual((counters["segments_dropped"], counters["segments_written"]), (1, 1))

    def test_a_segment_retention_removed_is_not_a_dropped_one(self):
        manager = RecordingManager()
        rows = [["segment-20260101T000030Z.mp4", "0", "10"]]
        root = Path(self.environment["NVR_RECORDINGS_PATH"])
        index = root / "index.csv"
        index.write_text("\n".join(",".join(row) for row in rows))
        manager._index_csv(self.d1, index, root / self.d1)  # Listed, but no such file any more.
        self.assertNotIn("segments_dropped", self.counters())

    def test_a_segment_and_its_counters_are_one_change(self):
        import sqlite3
        manager = RecordingManager()
        with mock.patch("app.metrics.bump", side_effect=sqlite3.OperationalError("database is locked")):
            self.index("segment-20260101T000040Z.mp4", b"x" * 500, [["segment-20260101T000040Z.mp4", "0", "10"]], manager)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recording_segments WHERE camera_id=?", (self.d1,)).fetchone()[0], 0)
        self.assertEqual(self.index("segment-20260101T000040Z.mp4", b"x" * 500, [["segment-20260101T000040Z.mp4", "0", "10"]], manager), 1)
        self.assertEqual(self.counters()["segments_written"], 1)

    def test_a_written_segment_is_counted_once_even_when_the_index_is_read_again(self):
        manager = RecordingManager()
        row = [["segment-20260101T000020Z.mp4", "0", "10"]]
        self.index("segment-20260101T000020Z.mp4", b"x" * 500, row, manager)
        self.index("segment-20260101T000020Z.mp4", b"x" * 500, row, manager)
        self.assertEqual(self.counters()["segments_written"], 1)

    def test_counting_never_disturbs_recording(self):
        import sqlite3
        with mock.patch("app.metrics.connect", side_effect=sqlite3.OperationalError("database is locked")):
            metrics.segment_dropped(self.d1)  # Does not raise.


class Collector:
    """A local syslog server over TCP, optionally TLS, that keeps what it is sent."""

    def __init__(self, context: ssl.SSLContext | None = None):
        self.frames: list[bytes] = []
        self.connections = 0
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port = self._listener.getsockname()[1]
        self._context = context
        self.drop_first = False
        self._stopped = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stopped:
            try:
                raw, _ = self._listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._read, args=(raw, self.connections), daemon=True).start()

    def _read(self, raw, number):
        try:
            connection = self._context.wrap_socket(raw, server_side=True) if self._context else raw
            buffer = b""
            while True:
                data = connection.recv(65536)
                if not data:
                    return
                buffer += data
                while True:
                    head, sep, rest = buffer.partition(b" ")
                    if not sep or not head.isdigit() or len(rest) < int(head):
                        break
                    if self.drop_first and number == 1:
                        connection.close()
                        return
                    self.frames.append(rest[:int(head)])
                    buffer = rest[int(head):]
        except (OSError, ssl.SSLError):
            return

    def stop(self):
        self._stopped = True
        self._listener.close()

    def wait_for(self, text: str, timeout: float = 15) -> bytes:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for frame in list(self.frames):
                if text.encode() in frame:
                    return frame
            time.sleep(0.05)
        raise AssertionError(f"{text!r} was never forwarded; got {[frame[:80] for frame in self.frames]}")


class OtlpCollector:
    def __init__(self):
        self.posts: list[tuple[str, dict[str, str], dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.posts.append((self.path, dict(self.headers), body))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def records(self) -> list[dict]:
        return [record for _, _, body in self.posts for resource in body["resourceLogs"] for scope in resource["scopeLogs"] for record in scope["logRecords"]]

    def wait_for(self, text: str, timeout: float = 15) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for record in self.records():
                if text in record["body"]["stringValue"]:
                    return record
            time.sleep(0.05)
        raise AssertionError(f"{text!r} was never forwarded")

    def stop(self):
        self.server.shutdown()


class ForwardCase(MetricsCase):
    def setUp(self):
        super().setUp()
        self.logger = logging.getLogger(f"nvr.test.{uuid.uuid4().hex[:8]}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.cleanups: list = []

    def tearDown(self):
        for cleanup in reversed(self.cleanups):
            cleanup()
        super().tearDown()

    def forward(self, url: str, level: int = logging.INFO, capacity: int = 2000, ca: str | None = None) -> logforward.Forwarder:
        forwarder = logforward.Forwarder(logforward.parse(url), "test", level, capacity, ca)
        forwarder.start()
        handler = logforward.ForwardHandler(forwarder)
        self.logger.addHandler(handler)
        self.cleanups.append(lambda: (self.logger.removeHandler(handler), forwarder.stop()))
        return forwarder

    def collector(self, context: ssl.SSLContext | None = None) -> Collector:
        collector = Collector(context)
        self.cleanups.append(collector.stop)
        return collector


class SyslogTests(ForwardCase):
    FRAME = re.compile(rb"^<(\d+)>1 (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z) (\S+) nvr-test (\d+) (\S+) - \xef\xbb\xbf(.*)$", re.S)

    def test_a_record_arrives_as_an_rfc_5424_message_over_tcp(self):
        collector = self.collector()
        self.forward(f"syslog+tcp://127.0.0.1:{collector.port}")
        self.logger.warning("recorder lost %s", "a segment")
        match = self.FRAME.match(collector.wait_for("recorder lost a segment"))
        self.assertIsNotNone(match)
        priority, stamp, host, pid, msgid, message = match.groups()
        self.assertEqual(int(priority), 3 * 8 + 4)  # Daemon facility, warning severity.
        self.assertEqual(host.decode(), socket.gethostname())
        self.assertEqual(int(pid), __import__("os").getpid())
        self.assertEqual(msgid.decode(), self.logger.name[:32])
        self.assertEqual(message.decode(), "recorder lost a segment")
        datetime.strptime(stamp.decode(), "%Y-%m-%dT%H:%M:%S.%fZ")

    def test_severity_maps_and_the_level_floor_applies(self):
        collector = self.collector()
        self.forward(f"syslog+tcp://127.0.0.1:{collector.port}", level=logging.WARNING)
        self.logger.info("quiet info")
        self.logger.error("loud error")
        self.logger.critical("fire")
        priorities = {int(self.FRAME.match(collector.wait_for(text)).group(1)) for text in ("loud error", "fire")}
        self.assertEqual(priorities, {3 * 8 + 3, 3 * 8 + 2})
        time.sleep(0.5)
        self.assertFalse(any(b"quiet info" in frame for frame in collector.frames))

    def test_a_multi_line_message_and_a_traceback_are_one_message(self):
        collector = self.collector()
        self.forward(f"syslog+tcp://127.0.0.1:{collector.port}")
        try:
            raise ValueError("boom")
        except ValueError:
            self.logger.exception("it failed")
        frame = collector.wait_for("it failed")
        self.assertIn(b"Traceback", frame)
        self.assertIn(b"ValueError: boom", frame)

    def test_tls_with_a_trusted_ca_delivers_and_an_untrusted_one_does_not(self):
        directory = Path(tempfile.mkdtemp())
        cert, key = self.certificate(directory)
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(cert, key)
        collector = self.collector(server)
        self.forward(f"syslog+tls://localhost:{collector.port}", ca=str(cert))
        self.logger.error("over tls")
        collector.wait_for("over tls")
        rejected = self.collector(server)
        untrusting = self.forward(f"syslog+tls://localhost:{rejected.port}")
        self.logger.error("to nobody trusted")
        end = time.monotonic() + 10
        while untrusting.failures == 0 and time.monotonic() < end:
            time.sleep(0.05)
        self.assertGreaterEqual(untrusting.failures, 1)
        self.assertFalse(any(b"nobody trusted" in frame for frame in rejected.frames))

    @staticmethod
    def certificate(directory: Path) -> tuple[Path, Path]:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=1)).not_valid_after(now + dt.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
        cert_path, key_path = directory / "cert.pem", directory / "key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        return cert_path, key_path

    def test_it_reconnects_after_the_collector_drops_the_connection(self):
        collector = self.collector()
        collector.drop_first = True
        forwarder = self.forward(f"syslog+tcp://127.0.0.1:{collector.port}")
        self.logger.error("first, lost")
        end = time.monotonic() + 10
        while forwarder.failures == 0 and collector.connections < 1 and time.monotonic() < end:
            time.sleep(0.05)
        time.sleep(0.3)
        self.logger.error("after reconnect")
        collector.wait_for("after reconnect")
        self.assertGreaterEqual(collector.connections, 2)


class OtlpTests(ForwardCase):
    def test_records_arrive_as_otlp_json_logs(self):
        collector = OtlpCollector()
        self.cleanups.append(collector.stop)
        self.forward(f"otlp+http://127.0.0.1:{collector.port}")
        self.logger.warning("export %s failed", "abc")
        record = collector.wait_for("export abc failed")
        self.assertEqual((record["severityText"], record["severityNumber"]), ("WARNING", 13))
        self.assertEqual(record["attributes"], [{"key": "logger", "value": {"stringValue": self.logger.name}}])
        self.assertGreater(int(record["timeUnixNano"]), time.time_ns() - 60 * 10**9)
        path, headers, body = collector.posts[0]
        self.assertEqual(path, "/v1/logs")
        self.assertEqual(headers["Content-Type"], "application/json")
        attributes = {item["key"]: item["value"].get("stringValue") for item in body["resourceLogs"][0]["resource"]["attributes"]}
        self.assertEqual((attributes["service.name"], attributes["nvr.process"]), ("nvr", "test"))
        pid = [item for item in body["resourceLogs"][0]["resource"]["attributes"] if item["key"] == "process.pid"][0]
        self.assertEqual(pid["value"], {"intValue": str(__import__("os").getpid())})
        self.assertGreater(int(record["observedTimeUnixNano"]), 0)

    def test_configured_headers_are_sent_with_every_request(self):
        collector = OtlpCollector()
        self.cleanups.append(collector.stop)
        forwarder = logforward.Forwarder(logforward.parse(f"otlp+http://127.0.0.1:{collector.port}"), "test", logging.INFO, 10, None, {"Authorization": "Bearer collector-token"})
        forwarder.start()
        handler = logforward.ForwardHandler(forwarder)
        self.logger.addHandler(handler)
        self.cleanups.append(lambda: (self.logger.removeHandler(handler), forwarder.stop()))
        self.logger.error("needs a token")
        collector.wait_for("needs a token")
        self.assertEqual(collector.posts[0][1]["Authorization"], "Bearer collector-token")

    def test_a_custom_path_is_used(self):
        collector = OtlpCollector()
        self.cleanups.append(collector.stop)
        self.forward(f"otlp+http://127.0.0.1:{collector.port}/custom/logs")
        self.logger.error("somewhere else")
        collector.wait_for("somewhere else")
        self.assertEqual(collector.posts[0][0], "/custom/logs")

    def test_a_collector_that_refuses_is_a_failure_not_a_crash(self):
        class Refusing(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Refusing)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.cleanups.append(server.shutdown)
        forwarder = self.forward(f"otlp+http://127.0.0.1:{server.server_address[1]}")
        self.logger.error("not accepted")
        end = time.monotonic() + 10
        while forwarder.failures == 0 and time.monotonic() < end:
            time.sleep(0.05)
        self.assertGreaterEqual(forwarder.failures, 1)


class ScrubbingTests(ForwardCase):
    def everything_forwarded(self) -> str:
        collector = self.collector()
        self.forward(f"syslog+tcp://127.0.0.1:{collector.port}")
        host, port = "192.0.2.50", 554
        self.logger.error("Connection to tcp://%s:%s failed", host, port)
        self.logger.error("could not open %s", STREAM)
        self.logger.error("bare host %s:%s refused, and %s too", host, port, "192.0.2.51")
        self.logger.error("camera ip 192.0.2.52 login cam-user-7 / onvif-pass-9 rejected")
        self.logger.error("password hunter2-secret was wrong for viewer")
        try:
            raise OSError(f"[Errno 111] Connection refused: {STREAM}")
        except OSError:
            self.logger.exception("stream failed")
        self.logger.error("the end")
        collector.wait_for("the end")
        return b"\n".join(collector.frames).decode("utf-8", "replace")

    def test_forwarded_logs_hold_no_camera_address_credential_or_stream_url(self):
        text = self.everything_forwarded()
        for secret in SECRETS:
            self.assertNotIn(secret, text, secret)
        self.assertIn("[redacted]", text)
        self.assertIn("Connection to [url redacted] failed", text)

    def test_the_rest_of_a_message_is_left_alone(self):
        text = self.everything_forwarded()
        for kept in ("stream failed", "Traceback (most recent call last)", "refused, and", "login", "rejected", "was wrong for"):
            self.assertIn(kept, text)

    def test_a_credential_written_as_a_setting_is_removed_whatever_it_is(self):
        collector = self.collector()
        self.forward(f"syslog+tcp://127.0.0.1:{collector.port}")
        self.logger.error("login failed password=Sup3r-Secret! for someone")
        self.logger.error('request had "token": "abc123def" and Authorization: Bearer eyJhbGciOi.payload.sig attached')
        self.logger.error("api_key=k-9999 and the token expired yesterday")
        self.logger.error("end of the credentials")
        collector.wait_for("end of the credentials")
        text = b"\n".join(collector.frames).decode()
        for leaked in ("Sup3r-Secret", "abc123def", "eyJhbGciOi", "k-9999"):
            self.assertNotIn(leaked, text)
        self.assertIn("password=[redacted] for someone", text)
        self.assertIn("the token expired yesterday", text)  # Ordinary prose is left alone.

    def test_a_camera_added_a_moment_ago_is_scrubbed_after_the_next_refresh(self):
        new = self.add_camera("Fresh")
        with connect() as db:
            db.execute("UPDATE cameras SET camera_ip='192.0.2.99' WHERE id=?", (new,))
        scrub = logforward.Scrubber()
        self.assertIn("192.0.2.99", scrub("seen 192.0.2.99"))  # Not known yet.
        scrub.refresh()
        self.assertEqual(scrub("seen 192.0.2.99 and 192.0.2.99:8080"), "seen [redacted] and [redacted]:8080")
        self.assertTrue(logforward.SECRETS_REFRESH_SECONDS <= 10)

    def test_a_very_long_message_is_cut(self):
        collector = self.collector()
        self.forward(f"syslog+tcp://127.0.0.1:{collector.port}")
        self.logger.error("long " + "x" * 50_000)
        frame = collector.wait_for("long xxx")
        self.assertLess(len(frame), logforward.MAX_MESSAGE + 200)

    def test_otlp_is_scrubbed_the_same_way(self):
        collector = OtlpCollector()
        self.cleanups.append(collector.stop)
        self.forward(f"otlp+http://127.0.0.1:{collector.port}")
        self.logger.error("failed %s as cam-user-7", STREAM)
        self.logger.error("marker")
        collector.wait_for("marker")
        text = json.dumps(collector.posts)
        for secret in SECRETS:
            self.assertNotIn(secret, text, secret)

    def test_metrics_hold_no_camera_address_or_credential_either(self):
        self.forward(f"syslog+tcp://127.0.0.1:{self.collector().port}")
        text = self.scrape()
        for secret in SECRETS:
            self.assertNotIn(secret, text, secret)
        self.assertEqual(self.samples(text, "nvr_log_forward_enabled")[0][1], 0)  # The test forwarder is not the process's.


class NeverBlockingTests(ForwardCase):
    def test_logging_does_not_wait_for_an_unreachable_endpoint(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()  # Nothing listens here now.
        forwarder = self.forward(f"syslog+tcp://127.0.0.1:{port}", capacity=50)
        began = time.monotonic()
        for index in range(5000):
            self.logger.error("line %s", index)
        self.assertLess(time.monotonic() - began, 2.0)
        end = time.monotonic() + 10
        while forwarder.dropped == 0 and time.monotonic() < end:
            time.sleep(0.05)
        self.assertGreater(forwarder.dropped, 0)

    def test_logging_does_not_wait_for_a_collector_that_accepts_and_never_reads(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.cleanups.append(listener.close)
        held = []
        threading.Thread(target=lambda: held.append(listener.accept()), daemon=True).start()
        self.forward(f"syslog+tcp://127.0.0.1:{listener.getsockname()[1]}", capacity=20)
        began = time.monotonic()
        for index in range(3000):
            self.logger.error("x" * 2000 + str(index))
        self.assertLess(time.monotonic() - began, 2.0)

    def test_a_full_queue_drops_and_counts_instead_of_waiting(self):
        forwarder = logforward.Forwarder(logforward.parse("syslog+tcp://127.0.0.1:9"), "test", logging.INFO, 3, None)  # Never started: nothing drains it.
        for index in range(10):
            forwarder.submit(logforward.Entry(time.time(), logging.INFO, "x", str(index)))
        self.assertEqual((forwarder.entries.qsize(), forwarder.dropped), (3, 7))

    def jammed(self) -> logforward.Forwarder:
        """Forwarding to a collector that accepts the connection and never reads, attached to the root logger."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        held = []
        threading.Thread(target=lambda: held.append(listener.accept()), daemon=True).start()
        forwarder = logforward.Forwarder(logforward.parse(f"syslog+tcp://127.0.0.1:{listener.getsockname()[1]}"), "test", logging.DEBUG, 50, None)
        forwarder.start()
        root = logging.getLogger()
        handler = logforward.ForwardHandler(forwarder)
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.cleanups += [listener.close, lambda: (root.removeHandler(handler), root.setLevel(previous), forwarder.stop())]
        return forwarder

    def test_requests_that_log_are_served_at_the_same_speed_while_the_collector_is_jammed(self):
        def timed() -> float:
            began = time.monotonic()
            for _ in range(15):
                self.assertEqual(self.admin.get("/api/cameras").status_code, 200)
                logging.getLogger("nvr.request").warning("handled a request %s", "x" * 4000)
            return time.monotonic() - began
        baseline = timed()
        forwarder = self.jammed()
        jammed = timed()
        self.assertLess(jammed, baseline * 3 + 1.0)
        for index in range(500):
            logging.getLogger("nvr.request").warning("%s %s", index, "y" * 4000)
        end = time.monotonic() + 15
        while forwarder.dropped == 0 and time.monotonic() < end:
            time.sleep(0.05)
        self.assertGreater(forwarder.dropped, 0)

    def test_recording_is_not_slowed_by_a_jammed_collector(self):
        self.jammed()
        manager = RecordingManager()
        root = Path(self.environment["NVR_RECORDINGS_PATH"])
        output = root / self.d1
        output.mkdir(parents=True, exist_ok=True)
        rows = []
        for index in range(40):
            name = f"segment-20260102T00{index:02d}00Z.mp4"
            (output / name).write_bytes(b"x" * 500)
            rows.append(f"{name},0,10")
        (root / "index.csv").write_text("\n".join(rows))
        began = time.monotonic()
        for _ in range(30):
            logging.getLogger("nvr.recording").warning("%s", "z" * 4000)
        self.assertEqual(manager._index_csv(self.d1, root / "index.csv", output), 40)
        self.assertLess(time.monotonic() - began, 5)

    def test_the_handler_never_raises_into_the_caller(self):
        class Unprintable:
            def __str__(self):
                raise RuntimeError("no")

        handler = logforward.ForwardHandler(logforward.Forwarder(logforward.parse("syslog+tcp://127.0.0.1:9"), "test", logging.INFO, 5, None))
        with mock.patch.object(logging, "raiseExceptions", False):
            handler.emit(logging.LogRecord("x", logging.ERROR, "f", 1, "%s", (Unprintable(),), None))

    def test_the_forwarders_own_trouble_is_not_forwarded_back_to_it(self):
        forwarder = logforward.Forwarder(logforward.parse("syslog+tcp://127.0.0.1:9"), "test", logging.DEBUG, 5, None)
        handler = logforward.ForwardHandler(forwarder)
        handler.handle(logging.LogRecord("nvr.logforward", logging.ERROR, "f", 1, "loop?", None, None))
        self.assertEqual(forwarder.entries.qsize(), 0)


class ConfigurationTests(unittest.TestCase):
    def tearDown(self):
        logforward._forwarder = None

    def test_the_urls_it_understands(self):
        cases = {"syslog+tcp://logs.example:514": ("syslog", "logs.example", 514, False), "syslog+tls://logs.example": ("syslog", "logs.example", 6514, True),
                 "otlp+http://collector:4318": ("otlp", "collector", 4318, False), "otlp+https://collector": ("otlp", "collector", 4318, True)}
        for url, expected in cases.items():
            target = logforward.parse(url)
            self.assertEqual((target.kind, target.host, target.port, target.tls), expected, url)
        self.assertEqual(logforward.parse("otlp+http://c:1/x/logs").path, "/x/logs")
        for bad in ("", "http://x", "syslog+udp://x:514", "syslog+tcp://", "garbage"):
            with self.assertRaises(ValueError, msg=bad):
                logforward.parse(bad)

    def test_nothing_is_installed_without_a_setting_and_a_bad_setting_is_ignored(self):
        with mock.patch.dict("os.environ", {"NVR_LOG_FORWARD_URL": ""}):
            self.assertIsNone(logforward.install("web"))
        with mock.patch.dict("os.environ", {"NVR_LOG_FORWARD_URL": "ftp://nope"}), self.assertLogs("nvr.logforward", "ERROR"):
            self.assertIsNone(logforward.install("web"))
        with mock.patch.dict("os.environ", {"NVR_LOG_FORWARD_URL": "syslog+tcp://127.0.0.1:9", "NVR_LOG_FORWARD_LEVEL": "LOUD"}), self.assertLogs("nvr.logforward", "ERROR"):
            self.assertIsNone(logforward.install("web"))

    def test_a_valid_setting_attaches_to_the_root_logger_once(self):
        root = logging.getLogger()
        before = list(root.handlers)
        with mock.patch.dict("os.environ", {"NVR_LOG_FORWARD_URL": "syslog+tcp://127.0.0.1:9", "NVR_LOG_FORWARD_LEVEL": "warning", "NVR_LOG_FORWARD_QUEUE": "10"}):
            forwarder = logforward.install("recorder")
            self.assertIs(logforward.install("recorder"), forwarder)
        try:
            self.assertEqual(len(root.handlers), len(before) + 1)
            self.assertEqual((forwarder.process, forwarder.level, forwarder.entries.maxsize), ("recorder", logging.WARNING, 10))
        finally:
            forwarder.stop()
            root.handlers[:] = before

    def test_the_supervisor_and_each_worker_install_it(self):
        for module in ("app/workers/supervisor.py", "app/workers/runtime.py", "app/workers/web.py"):
            self.assertIn("logforward.install(", Path(module).read_text(), module)


class WiringTests(ForwardCase):
    """The real installation, as a worker does it: the application's own loggers and uvicorn's reach the collector."""

    def setUp(self):
        super().setUp()
        self.root = logging.getLogger()
        before_handlers, before_level = list(self.root.handlers), self.root.level
        self.cleanups.append(lambda: (self.root.handlers.__setitem__(slice(None), before_handlers), self.root.setLevel(before_level),
                                      setattr(logforward, "_forwarder", None)))
        logforward._forwarder = None

    def installed(self, process: str, collector: Collector, **extra: str) -> logforward.Forwarder:
        with mock.patch.dict("os.environ", {"NVR_LOG_FORWARD_URL": f"syslog+tcp://127.0.0.1:{collector.port}", **extra}):
            forwarder = logforward.install(process)
        self.assertIsNotNone(forwarder)
        self.cleanups.append(forwarder.stop)
        return forwarder

    def test_an_application_logger_at_info_is_delivered_from_a_process_that_never_configured_logging(self):
        self.root.setLevel(logging.NOTSET)  # What the web process starts with.
        collector = self.collector()
        self.installed("web", collector)
        logging.getLogger("nvr.recording").info("recorder says hello")
        frame = collector.wait_for("recorder says hello")
        self.assertIn(b" nvr-web ", frame)

    def test_uvicorns_error_log_is_delivered_after_uvicorn_configures_logging(self):
        import logging.config
        from app.workers import web
        collector = self.collector()
        self.installed("web", collector)
        logging.config.dictConfig(web.log_config())  # What uvicorn.run does at start-up.
        logging.getLogger("uvicorn.error").error("Exception in ASGI application: %s", STREAM)
        logging.getLogger("nvr.after").warning("still delivered after uvicorn's setup")
        frame = collector.wait_for("Exception in ASGI application")
        collector.wait_for("still delivered after uvicorn's setup")
        self.assertNotIn(b"hunter2", frame)
        self.assertIn(b"[url redacted]", frame)

    def test_the_access_log_is_not_forwarded(self):
        import logging.config
        from app.workers import web
        collector = self.collector()
        self.installed("web", collector)
        logging.config.dictConfig(web.log_config())
        logging.getLogger("uvicorn.access").info('127.0.0.1:1234 - "GET /api/cameras?x=1 HTTP/1.1" 200')
        logging.getLogger("nvr.after").warning("marker after the access line")
        collector.wait_for("marker after the access line")
        self.assertFalse(any(b"GET /api/cameras" in frame for frame in collector.frames))

    def test_headers_come_from_the_environment(self):
        collector = OtlpCollector()
        self.cleanups.append(collector.stop)
        with mock.patch.dict("os.environ", {"NVR_LOG_FORWARD_URL": f"otlp+http://127.0.0.1:{collector.port}", "NVR_LOG_FORWARD_HEADERS": "Authorization=Bearer abc,X-Tenant=nvr"}):
            forwarder = logforward.install("web")
        self.cleanups.append(forwarder.stop)
        logging.getLogger("nvr.x").warning("with headers")
        collector.wait_for("with headers")
        headers = collector.posts[0][1]
        self.assertEqual((headers["Authorization"], headers["X-Tenant"]), ("Bearer abc", "nvr"))

    def test_what_is_queued_goes_out_at_exit(self):
        collector = self.collector()
        forwarder = self.installed("web", collector)
        for index in range(30):
            logging.getLogger("nvr.exit").error("closing line %s", index)
        forwarder.flush(10)
        self.assertEqual(forwarder.entries.unfinished_tasks, 0)
        collector.wait_for("closing line 29", timeout=5)

    def test_a_tls_connection_is_kept_between_batches(self):
        directory = Path(tempfile.mkdtemp())
        cert, key = SyslogTests.certificate(directory)
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(cert, key)
        collector = self.collector(server)
        self.forward(f"syslog+tls://localhost:{collector.port}", ca=str(cert))
        for index in range(4):
            self.logger.error("tls batch %s", index)
            collector.wait_for(f"tls batch {index}")
            time.sleep(0.3)
        self.assertEqual(collector.connections, 1)


if __name__ == "__main__":
    unittest.main()
