"""An NVR that takes part in failover, for tests of the failover recorder (#143): the production application with its failover manager (and the footage chain) running,
and, when asked, a stand-in recorder that writes a small footage file every second for each camera the real recording manager decides to record.

The decision whether to record is the production one (``RecordingManager.sync_camera`` and the failover gate); only the ffmpeg pull from a camera is replaced.
Served by ``uvicorn tests.fixtures.failover_app:app``; the peer is on this machine, which the address check refuses, so that check is the one thing relaxed.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from app import failover
from app.application import create_app
from app.footage_chain import FootageChainManager
from app.recording import RecordingManager, recordings_path
from app.storage import camera_recordings_dir
from app.store import connect

failover.check_peer_url = lambda url, allow_private: url.rstrip("/")  # type: ignore[assignment]


async def _stand_in_run(self: RecordingManager, camera_id: str, mode: str) -> None:
    """Records a camera as the real recorder would be seen to: status 'recording', a footage file and its index row every second, status 'stopped' when told to stop."""
    self._set_status(camera_id, "recording")
    try:
        while True:
            now = datetime.now(timezone.utc)
            directory = camera_recordings_dir(camera_id)
            directory.mkdir(parents=True, exist_ok=True)
            name = f"segment-{now:%Y%m%dT%H%M%S%f}Z.mp4"
            path = directory / name
            path.write_bytes(f"footage {camera_id} {now.isoformat()} ".encode() * 40)
            relative = path.resolve().relative_to(recordings_path()).as_posix()
            with connect() as db:
                db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes,source) VALUES(?,?,?,?,?,'live')",
                           (camera_id, relative, (now - timedelta(seconds=1)).isoformat(), now.isoformat(), path.stat().st_size))
                db.execute("UPDATE cameras SET last_video_at=?,last_footage_write_at=? WHERE id=?", (now.isoformat(), now.isoformat(), camera_id))
            await asyncio.sleep(1.0)
    finally:
        self._set_status(camera_id, "stopped")


app = create_app(start_workers=False)
_inner = app.router.lifespan_context


@asynccontextmanager
async def _lifespan(application):
    async with _inner(application):
        started = []
        failover.prepare_startup()
        chain = FootageChainManager(poll_seconds=0.5, sign_seconds=1.0)
        await chain.start()
        started.append(chain)
        if os.environ.get("NVR_TEST_FAKE_RECORDER") == "1":
            RecordingManager._run = _stand_in_run  # type: ignore[assignment]
            recorder = RecordingManager()
            await recorder.start()
            started.append(recorder)
        manager = failover.FailoverManager(tick_seconds=float(os.environ.get("NVR_TEST_FAILOVER_TICK", "0.5")))
        await manager.start()
        started.append(manager)
        try:
            yield
        finally:
            for item in reversed(started):
                await item.stop()


app.router.lifespan_context = _lifespan
