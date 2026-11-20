"""The container's health check gives start-up long enough, and start-up says how long it took (#418).

Before a schema change the NVR copies its database (and backs it up when backups are on), and answers nothing, /healthz included, until that
is done. On the live NVR an upgrade with a 1.2 GB database took about a minute and a half, past the old 10 s start period plus three 30 s
checks, so Docker reported the new container unhealthy for half a minute although nothing was wrong."""
from __future__ import annotations

import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
UPGRADE_ALLOWANCE_SECONDS = 300


def _seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)s", value.strip())
    assert match, value
    return int(match.group(1))


class HealthCheckTests(unittest.TestCase):
    def test_the_image_and_compose_health_checks_agree_and_allow_for_an_upgrade(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        flags = dict(re.findall(r"--([a-z-]+)=(\S+)", re.search(r"^HEALTHCHECK (.*?)\\$", dockerfile, re.M).group(1)))
        compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]["nvr"]["healthcheck"]
        image = {"interval": _seconds(flags["interval"]), "timeout": _seconds(flags["timeout"]), "start_period": _seconds(flags["start-period"]), "retries": int(flags["retries"])}
        stack = {"interval": _seconds(compose["interval"]), "timeout": _seconds(compose["timeout"]), "start_period": _seconds(compose["start_period"]), "retries": int(compose["retries"])}
        self.assertEqual(image, stack)  # compose.yaml replaces the image's check, so the two must not drift apart
        self.assertGreaterEqual(image["start_period"], UPGRADE_ALLOWANCE_SECONDS)
        self.assertEqual(image["interval"], 30)  # A running NVR that stops answering is still reported within a minute and a half


class StartupTimingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.environment = {"NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_RECORDINGS_PATH": str(root / "recordings"), "NVR_CREDENTIAL_KEY_PATH": str(root / "credentials.key"),
                            "NVR_STARTED_AT": repr(time.time() - 12.0)}
        (root / "recordings").mkdir()
        self.patcher = mock.patch.dict(os.environ, self.environment)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temporary.cleanup()

    def test_the_web_interface_says_how_long_after_the_nvr_started_it_first_answered(self):
        from app.application import create_app

        with self.assertLogs("uvicorn.error", level="INFO") as logged, TestClient(create_app(start_workers=False)) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
        line = next(line for line in logged.output if "web interface is answering" in line)
        seconds = float(re.search(r"answering, ([\d.]+) s after the NVR started", line).group(1))
        self.assertGreaterEqual(seconds, 12.0)
        self.assertLess(seconds, 12.0 + 60)


if __name__ == "__main__":
    unittest.main()
