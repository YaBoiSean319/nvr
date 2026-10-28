"""Real NVRs, as separate processes with their own databases, for tests of the multi-site console (#141)."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "site-admin-password-1234"


class NvrSite:
    """One NVR (a site): its own database and recordings, an administrator, and a port. ``seed`` gives it cameras, an alarm and a track."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        (base / "recordings").mkdir()
        self.env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "NVR_DB_PATH": str(base / "state.sqlite3"), "NVR_RECORDINGS_PATH": str(base / "recordings"),
                    "NVR_CREDENTIAL_KEY_PATH": str(base / "credentials.key"), "NVR_COOKIE_SECURE": "false"}
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen | None = None
        self.log = open(base / "server.log", "wb")
        self.made: dict = {}
        self._admin: httpx.Client | None = None

    def start(self) -> "NvrSite":
        self.process = subprocess.Popen([sys.executable, "-m", "uvicorn", "tests.fixtures.site_app:app", "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
                                        cwd=ROOT, env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(150):
            try:
                if httpx.get(f"{self.url}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise RuntimeError(f"The site {self.name} did not start: {Path(self.directory.name, 'server.log').read_text()[-800:]}")
        return self

    def admin(self) -> httpx.Client:
        if self._admin is None:
            client = httpx.Client(base_url=self.url, timeout=10)
            created = client.post("/api/setup/admin", json={"username": f"{self.name}-admin", "password": PASSWORD})
            assert created.status_code == 201, created.text
            self._admin = client
        return self._admin

    def seed(self, cameras: list[str], **extra) -> dict:
        self.admin()
        result = subprocess.run([sys.executable, "-m", "tests.fixtures.seed_site", json.dumps({"cameras": cameras, **extra})], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        self.made = json.loads(result.stdout.strip().splitlines()[-1])
        return self.made

    def token(self, scopes: list[str]) -> str:
        response = self.admin().post("/api/admin/api-tokens", json={"name": f"console-{'-'.join(scopes)}", "scopes": scopes})
        assert response.status_code == 201, response.text
        return response.json()["secret"]

    def pause(self) -> None:
        """The site stops answering but keeps its port open, as a hung machine does."""
        assert self.process is not None
        self.process.send_signal(signal.SIGSTOP)

    def resume(self) -> None:
        assert self.process is not None
        self.process.send_signal(signal.SIGCONT)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGCONT)
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self._admin is not None:
            self._admin.close()
        self.log.close()
        self.directory.cleanup()
