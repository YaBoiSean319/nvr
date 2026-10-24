from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from app.workers import control
from tests.test_support import TEST_PASSWORD

ROOT = Path(__file__).resolve().parents[1]
UNREACHABLE_STREAM = "rtsp://127.0.0.1:9/unreachable"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait(predicate, timeout: float, message: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.25)
    raise AssertionError(message)


class SupervisedInstallation:
    """The real container entrypoint against an isolated data directory."""

    def __init__(self, root: Path, **environment: str) -> None:
        self.root = root
        self.port = _free_port()
        self.database = root / "nvr.sqlite3"
        self.environment = {
            **os.environ,
            "NVR_DB_PATH": str(self.database),
            "NVR_RECORDINGS_PATH": str(root / "recordings"),
            "NVR_CREDENTIAL_KEY_PATH": str(root / "credentials.key"),
            "NVR_MOTION_BUFFER_PATH": str(root / "motion-buffer"),
            "NVR_LISTEN_PORT": str(self.port),
            "NVR_STOP_GRACE_SECONDS": "10",
            **environment,
        }
        self.environment.pop("NVR_SUPERVISED", None)
        self.log = open(root / "supervisor.log", "wb")
        self.process = subprocess.Popen([sys.executable, "-m", "app.entrypoint"], cwd=ROOT, env=self.environment, stdout=self.log, stderr=subprocess.STDOUT)
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        _wait(self._healthy, 30, "the supervised web worker did not become healthy")

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=1) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=10) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else {}
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def query(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            return db.execute(sql, parameters).fetchall()
        finally:
            db.close()

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        try:
            db.execute(sql, parameters)
        finally:
            db.close()

    def worker(self, name: str) -> sqlite3.Row:
        return self.query("SELECT * FROM worker_status WHERE name=?", (name,))[0]

    def running_pid(self, name: str, other_than: int | None = None) -> int | bool:
        rows = self.query("SELECT pid,state,heartbeat_at FROM worker_status WHERE name=?", (name,))
        if rows and rows[0]["state"] == "running" and rows[0]["heartbeat_at"] and rows[0]["pid"] != other_than:
            return rows[0]["pid"]
        return False

    def incident(self, fault_type: str) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM incidents WHERE fault_type=?", (fault_type,))
        return rows[0] if rows else None

    def stop(self) -> int:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.log.close()
        return self.process.returncode


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.nvr = SupervisedInstallation(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.nvr.stop)
        status, _ = self.nvr.request("POST", "/api/setup/admin", {"username": "keeper", "password": TEST_PASSWORD})
        self.assertEqual(status, 201)

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_status_reports_each_worker_and_recorder_crash_is_restarted_with_incident(self):
        recorder = _wait(lambda: self.nvr.running_pid("recorder"), 15, "recorder never reported liveness")
        _wait(lambda: self.nvr.running_pid("web"), 15, "web never reported liveness")
        status, system = self.nvr.request("GET", "/api/system/status")
        self.assertEqual(status, 200)
        self.assertEqual(system["application_status"], "healthy")
        for name in ("exporter", "ai", "background"):
            _wait(lambda: self.nvr.running_pid(name), 15, f"{name} never reported liveness")
        status, system = self.nvr.request("GET", "/api/system/status")
        self.assertEqual({worker["name"]: worker["alive"] for worker in system["workers"]}, {name: True for name in ("web", "recorder", "exporter", "ai", "background")})

        os.kill(recorder, signal.SIGKILL)
        raised = _wait(lambda: self.nvr.incident("worker_recorder"), 10, "recorder crash raised no incident")
        self.assertEqual(raised["camera_id"], None)
        replacement = _wait(lambda: self.nvr.running_pid("recorder", other_than=recorder), 20, "recorder was not restarted")
        self.assertNotEqual(replacement, recorder)
        self.assertEqual(self.nvr.worker("recorder")["restarts"], 1)
        self.assertEqual(self.nvr.worker("recorder")["last_exit_code"], -signal.SIGKILL)
        recovered = _wait(lambda: (row := self.nvr.incident("worker_recorder")) and row["state"] == "recovered" and row, 10, "incident did not recover")
        self.assertEqual(recovered["recurrence_count"], 1)
        events = [row["event_type"] for row in self.nvr.query("SELECT event_type FROM incident_events ORDER BY occurred_at")]
        self.assertEqual(events, ["raised", "recovered"])

    def test_web_crash_leaves_the_recorder_running(self):
        recorder = _wait(lambda: self.nvr.running_pid("recorder"), 15, "recorder never reported liveness")
        web = _wait(lambda: self.nvr.running_pid("web"), 15, "web never reported liveness")
        os.kill(web, signal.SIGKILL)
        _wait(lambda: self.nvr.running_pid("web", other_than=web), 30, "web was not restarted")
        _wait(self.nvr._healthy, 15, "web did not become healthy again")
        self.assertEqual(self.nvr.worker("recorder")["pid"], recorder)
        self.assertTrue(self._pid_alive(recorder))
        self.assertEqual(self.nvr.worker("recorder")["restarts"], 0)
        self.assertIsNotNone(self.nvr.incident("worker_web"))

    def test_configuration_changes_reach_the_recorder_by_nudge_and_by_reconciliation(self):
        _wait(lambda: self.nvr.running_pid("recorder"), 15, "recorder never reported liveness")
        from app.credentials import encrypt

        previous = os.environ.get("NVR_CREDENTIAL_KEY_PATH")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = self.nvr.environment["NVR_CREDENTIAL_KEY_PATH"]
        try:
            ciphertext = encrypt(UNREACHABLE_STREAM)
        finally:
            if previous is None:
                os.environ.pop("NVR_CREDENTIAL_KEY_PATH", None)
            else:
                os.environ["NVR_CREDENTIAL_KEY_PATH"] = previous
        self.nvr.execute(
            "INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,recording_enabled,recording_status,created_at,updated_at) "
            "VALUES('cam-1','Dock',?, 'continuous',0,'stopped',strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (ciphertext,),
        )

        def status() -> str:
            return self.nvr.query("SELECT recording_status FROM cameras WHERE id='cam-1'")[0]["recording_status"]

        changed, _ = self.nvr.request("PATCH", "/api/cameras/cam-1", {"recording_enabled": True})
        self.assertEqual(changed, 200)
        _wait(lambda: status() != "stopped", 3, "the nudged recorder did not start recording within the nudge window")

        # A change committed without any nudge is still applied by reconciliation.
        self.nvr.execute("UPDATE cameras SET recording_enabled=0 WHERE id='cam-1'")
        _wait(lambda: status() == "stopped", 15, "reconciliation did not apply an un-nudged change")

    def _footage(self, camera_id: str, seconds: int) -> Path:
        recordings = Path(self.nvr.environment["NVR_RECORDINGS_PATH"])
        source = recordings / camera_id / "clip.mp4"
        source.parent.mkdir(parents=True)
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=640x360:r=15",
            "-t", str(seconds), "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-y", str(source),
        ], check=True, timeout=120)
        self.nvr.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (camera_id, f"Yard {camera_id}"))
        self.nvr.execute(
            "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
            (camera_id, f"{camera_id}/clip.mp4", "2026-10-20T10:00:00+00:00", f"2026-10-20T10:{seconds // 60:02d}:{seconds % 60:02d}+00:00", source.stat().st_size),
        )
        return source

    def _export(self, camera_id: str, seconds: int, **options) -> dict:
        created, job = self.nvr.request("POST", "/api/exports", {
            "camera_ids": [camera_id], "start": "2026-10-20T10:00:00Z",
            "end": f"2026-10-20T10:{seconds // 60:02d}:{seconds % 60:02d}Z", **options,
        })
        self.assertEqual(created, 202, job)
        return job

    def _export_status(self, job_id: str) -> str:
        return self.nvr.query("SELECT status FROM exports WHERE id=?", (job_id,))[0]["status"]

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required to render an export")
    def test_exports_complete_and_cancel_through_the_export_worker(self):
        _wait(lambda: self.nvr.running_pid("exporter"), 15, "exporter never reported liveness")
        self._footage("cam-short", 2)
        done = self._export("cam-short", 2)
        _wait(lambda: self._export_status(done["id"]) in {"completed", "failed"}, 60, "the export never finished")
        status, finished = self.nvr.request("GET", f"/api/exports/{done['id']}")
        self.assertEqual(finished["status"], "completed", finished)

        self._footage("cam-long", 240)
        running = self._export("cam-long", 240, grid_resolution="4k")
        _wait(lambda: self._export_status(running["id"]) == "running", 20, "the export never started rendering")
        cancelled, _ = self.nvr.request("POST", f"/api/exports/{running['id']}/cancel")
        self.assertEqual(cancelled, 200)
        _wait(lambda: self._export_status(running["id"]) == "cancelled", 20, "the running export was not cancelled")

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required to render an export")
    def test_exporter_crash_fails_a_running_export_truthfully_and_releases_its_sources(self):
        _wait(lambda: self.nvr.running_pid("exporter"), 15, "exporter never reported liveness")
        source = self._footage("cam-x", 240)
        recordings = Path(self.nvr.environment["NVR_RECORDINGS_PATH"])
        job = self._export("cam-x", 240, grid_resolution="4k")
        _wait(lambda: self._export_status(job["id"]) == "running", 20, "the export never started rendering")
        exporter = self.nvr.worker("exporter")["pid"]
        os.kill(exporter, signal.SIGKILL)
        _wait(lambda: self.nvr.running_pid("exporter", other_than=exporter), 20, "exporter was not restarted")
        row = _wait(lambda: (rows := self.nvr.query("SELECT * FROM exports WHERE id=?", (job["id"],))) and rows[0]["status"] != "running" and rows[0], 10, "the interrupted export stayed running")
        self.assertEqual(row["status"], "failed")
        self.assertIn("interrupted", row["error"])
        self.assertIsNone(row["output_relative_path"])
        self.assertFalse(list((recordings / "exports").glob(f"*{job['id']}*")))
        from app.retention import playback_is_leased

        self.assertFalse(playback_is_leased(source))
        self.assertIsNotNone(self.nvr.incident("worker_exporter"))

    def test_ai_crash_marks_analysis_interrupted_without_touching_recording(self):
        recorder = _wait(lambda: self.nvr.running_pid("recorder"), 15, "recorder never reported liveness")
        ai = _wait(lambda: self.nvr.running_pid("ai"), 15, "ai never reported liveness")
        self.nvr.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES('cam-a','Gate',X'01','disabled')")
        self.nvr.execute("INSERT INTO ai_camera_settings(camera_id,status) VALUES('cam-a','analyzed')")
        os.kill(ai, signal.SIGKILL)
        incident = _wait(lambda: self.nvr.incident("ai_worker"), 10, "AI crash raised no incident")
        self.assertIn("AI analysis process stopped", incident["summary"])
        setting = self.nvr.query("SELECT status,last_error FROM ai_camera_settings WHERE camera_id='cam-a'")[0]
        self.assertEqual(setting["status"], "interrupted")
        coverage = self.nvr.query("SELECT state FROM ai_coverage WHERE camera_id='cam-a' AND ends_at IS NULL")
        self.assertEqual([row["state"] for row in coverage], ["unanalyzed"])
        _wait(lambda: self.nvr.running_pid("ai", other_than=ai), 20, "ai was not restarted")
        self.assertEqual(self.nvr.worker("recorder")["pid"], recorder)

    def test_background_worker_attempts_webhook_deliveries(self):
        _wait(lambda: self.nvr.running_pid("background"), 15, "background never reported liveness")
        created, body = self.nvr.request("POST", "/api/webhooks", {
            "name": "Unroutable receiver", "url": "http://192.0.2.1:9/hook", "events": ["system.failure"], "allow_private_receiver": True,
        })
        self.assertEqual(created, 201, body)
        self.assertEqual(self.nvr.request("POST", f"/api/webhooks/{body['webhook']['id']}/test")[0], 202)
        attempted = _wait(
            lambda: (rows := self.nvr.query("SELECT attempt_count FROM webhook_deliveries")) and rows[0]["attempt_count"] >= 1 and rows,
            30, "the background worker never attempted the delivery",
        )
        self.assertEqual(len(attempted), 1)

    def test_stop_drains_every_worker(self):
        pids = [
            _wait(lambda name=name: self.nvr.running_pid(name), 15, f"{name} never reported liveness")
            for name in ("recorder", "exporter", "ai", "background", "web")
        ]
        started = time.monotonic()
        self.assertEqual(self.nvr.stop(), 0)
        self.assertLess(time.monotonic() - started, 15)
        for pid in pids:
            self.assertFalse(self._pid_alive(pid))
        self.assertEqual({row["name"]: row["state"] for row in self.nvr.query("SELECT name,state FROM worker_status")}, {name: "stopped" for name in ("web", "recorder", "exporter", "ai", "background")})


@unittest.skipUnless(os.environ.get("NVR_RTSP_TEST_URL"), "A simulated RTSP publisher is required")
class RecordingContinuityTests(unittest.TestCase):
    def test_recording_continues_while_the_web_process_restarts(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        nvr = SupervisedInstallation(Path(temporary.name), NVR_SEGMENT_SECONDS="10")
        self.addCleanup(nvr.stop)
        self.assertEqual(nvr.request("POST", "/api/setup/admin", {"username": "keeper", "password": TEST_PASSWORD})[0], 201)
        created, camera = nvr.request("POST", "/api/cameras", {"name": "Continuity", "stream_url": os.environ["NVR_RTSP_TEST_URL"], "recording_mode": "continuous"})
        self.assertEqual(created, 201, camera)
        self.assertEqual(nvr.request("PATCH", f"/api/cameras/{camera['id']}", {"recording_enabled": True})[0], 200)
        recorder = _wait(lambda: nvr.running_pid("recorder"), 15, "recorder never reported liveness")

        def segments() -> list[sqlite3.Row]:
            return nvr.query("SELECT starts_at,ends_at FROM recording_segments WHERE camera_id=? ORDER BY starts_at", (camera["id"],))

        _wait(lambda: len(segments()) >= 1, 40, "no segment was written before the web restart")
        web = nvr.worker("web")["pid"]
        killed_at = time.time()
        os.kill(web, signal.SIGKILL)
        _wait(lambda: len(segments()) >= 3, 60, "segments stopped being written while the web process restarted")
        self.assertEqual(nvr.worker("recorder")["pid"], recorder)
        written = segments()
        self.assertGreater(written[-1]["ends_at"], time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(killed_at)))
        from datetime import datetime

        def parse(value: str) -> datetime:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))

        for before, after in zip(written, written[1:]):
            self.assertLess((parse(after["starts_at"]) - parse(before["ends_at"])).total_seconds(), 2.0)


class ControlChannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("NVR_CONTROL_DIR")
        os.environ["NVR_CONTROL_DIR"] = str(Path(self.temporary.name) / "run")

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("NVR_CONTROL_DIR", None)
        else:
            os.environ["NVR_CONTROL_DIR"] = self.previous
        self.temporary.cleanup()

    def test_recorder_client_delivers_nudges_and_tolerates_a_missing_worker(self):
        async def scenario() -> list[dict]:
            self.assertFalse(await control.nudge("recorder", {"op": "sync_camera", "camera_id": "c1"}))
            await control.RecorderClient().sync_camera("c1")  # never raises without a worker
            received: list[dict] = []
            arrived = asyncio.Event()

            async def handle(message: dict) -> None:
                received.append(message)
                arrived.set()

            server = await control.serve("recorder", handle)
            try:
                self.assertEqual(control.socket_path("recorder").stat().st_mode & 0o777, 0o600)
                await control.RecorderClient().sync_camera("c1", force_restart=True)
                # The client returns only after the worker has handled the nudge.
                self.assertTrue(arrived.is_set())
                self.assertTrue(await control.nudge("recorder", {"op": "noop"}))
            finally:
                server.close()
                await server.wait_closed()
            return received

        self.assertEqual(asyncio.run(scenario()), [{"op": "sync_camera", "camera_id": "c1", "force_restart": True}, {"op": "noop"}])


if __name__ == "__main__":
    unittest.main()
