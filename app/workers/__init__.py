"""Supervised background processes.

The supervisor (``app.workers.supervisor``) starts each worker as its own process,
restarts it with bounded backoff, and raises an incident while it is down. The
database is the source of truth for configuration; the control channel only
nudges a worker to act sooner than its periodic reconciliation would.
"""
from __future__ import annotations

import os
from pathlib import Path

from app.store import database_path


def supervised() -> bool:
    """Whether this process runs under the supervisor rather than in-process (tests, development)."""
    return os.environ.get("NVR_SUPERVISED") == "1"


def control_directory() -> Path:
    return Path(os.environ.get("NVR_CONTROL_DIR", str(database_path().parent / "run")))
