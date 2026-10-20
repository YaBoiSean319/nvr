from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .onvif_events import CameraMotionEvent
from .store import connect


@dataclass(frozen=True)
class HlsSegment:
    name: str
    starts_at: datetime
    duration: float

    @property
    def ends_at(self) -> datetime:
        return self.starts_at + timedelta(seconds=self.duration)


def parse_live_playlist(text: str) -> list[HlsSegment]:
    """Parse only local, generated HLS segment names and program timestamps."""
    segments: list[HlsSegment] = []
    next_start: datetime | None = None
    duration: float | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            raw = line.partition(":")[2]
            try:
                next_start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if next_start.tzinfo is None:
                    next_start = next_start.replace(tzinfo=timezone.utc)
                next_start = next_start.astimezone(timezone.utc)
            except ValueError:
                next_start = None
        elif line.startswith("#EXTINF:"):
            try:
                duration = float(line.partition(":")[2].split(",", 1)[0])
            except ValueError:
                duration = None
        elif line and not line.startswith("#") and duration is not None:
            name = Path(line).name
            if Path(line).name != line or not name.startswith("segment-") or not name.endswith(".ts"):
                duration = None
                continue
            if next_start is not None and 0 < duration <= 30:
                segments.append(HlsSegment(name, next_start, duration))
                next_start += timedelta(seconds=duration)
            duration = None
    return segments


@dataclass
class _Capture:
    event_id: str
    first_received_at: datetime
    requested_start: datetime
    first_source_event_at: datetime
    capture_until: datetime
    active: bool = True
    source_events: list[dict[str, str | bool]] = field(default_factory=list)
    copied: set[str] = field(default_factory=set)
    segments: dict[str, HlsSegment] = field(default_factory=dict)
    staging_dir: Path | None = None
    last_segment_end: datetime | None = None


class MotionClipAssembler:
    """Turn validated motion transitions and a rolling HLS buffer into indexed MP4 clips."""

    def __init__(self, camera_id: str, spool_dir: Path, pre_roll_seconds: int = 10, post_roll_seconds: int = 30):
        self.camera_id = camera_id
        self.spool_dir = spool_dir
        self.pre_roll_seconds = max(0, min(int(pre_roll_seconds), 120))
        self.post_roll_seconds = max(1, min(int(post_roll_seconds), 600))
        self.capture: _Capture | None = None
        self.segment_seconds = 2
        self._lock = asyncio.Lock()

    async def ingest(self, event: CameraMotionEvent) -> None:
        async with self._lock:
            await self._ingest(event)

    async def _ingest(self, event: CameraMotionEvent) -> None:
        received = datetime.now(timezone.utc)
        capture = self.capture
        if event.active and capture is not None and not capture.active and received > capture.capture_until:
            # A notification may race the periodic polling loop. Finalize the
            # expired clip before starting another; if its end is not yet
            # buffered, keep the current capture and merge the transition.
            await self._poll()
            capture = self.capture
        if event.active:
            if capture is None:
                capture = _Capture(
                    event_id=str(uuid.uuid4()), first_received_at=received,
                    requested_start=received - timedelta(seconds=self.pre_roll_seconds),
                    first_source_event_at=event.event_time,
                    capture_until=received + timedelta(seconds=self.post_roll_seconds),
                    staging_dir=self.spool_dir / ("capture-" + uuid.uuid4().hex),
                )
                capture.staging_dir.mkdir(parents=True, exist_ok=True)
                self.capture = capture
                with connect() as db:
                    db.execute(
                        "INSERT INTO motion_events(id,camera_id,source,source_topic,source_event_at,received_at,requested_start,requested_end,pre_roll_complete,status,source_events_json) "
                        "VALUES(?,?,?,?,?,?,?,?,0,'recording','[]')",
                        (capture.event_id, self.camera_id, event.source, event.topic, event.event_time.isoformat(), received.isoformat(),
                         capture.requested_start.isoformat(), capture.capture_until.isoformat()),
                    )
            else:
                capture.active = True
                capture.capture_until = max(capture.capture_until, received + timedelta(seconds=self.post_roll_seconds))
        elif capture is not None:
            capture.active = False
            capture.capture_until = max(capture.capture_until, received + timedelta(seconds=self.post_roll_seconds))
        if capture is None:
            return
        if len(capture.source_events) < 128:
            capture.source_events.append({"source": event.source, "source_event_at": event.event_time.isoformat(), "received_at": received.isoformat(), "topic": event.topic, "active": event.active})
        with connect() as db:
            db.execute("UPDATE motion_events SET requested_end=?,source_events_json=? WHERE id=?",
                       (capture.capture_until.isoformat(), json.dumps(capture.source_events, separators=(",", ":")), capture.event_id))

    async def poll(self) -> None:
        async with self._lock:
            await self._poll()

    async def _poll(self) -> None:
        capture = self.capture
        if capture is None:
            return
        playlist_path = self.spool_dir / "live.m3u8"
        try:
            segments = parse_live_playlist(playlist_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return
        now = datetime.now(timezone.utc)
        eligible = [s for s in segments if s.ends_at >= capture.requested_start and s.starts_at <= now]
        for index, segment in enumerate(eligible):
            if segment.name in capture.copied:
                continue
            source = self.spool_dir / segment.name
            if not source.is_file():
                continue
            destination = capture.staging_dir / f"part-{len(capture.copied):06d}.ts"
            try:
                shutil.copyfile(source, destination)
            except OSError:
                continue
            capture.copied.add(segment.name)
            capture.segments[segment.name] = segment
            capture.last_segment_end = max(capture.last_segment_end or segment.ends_at, segment.ends_at)
        if capture.active or now < capture.capture_until or not capture.copied:
            return
        if capture.last_segment_end and capture.last_segment_end < capture.capture_until:
            return
        await self._finalize(capture)
        self.capture = None

    async def _finalize(self, capture: _Capture) -> None:
        from .recording import recordings_path
        staging = capture.staging_dir
        output_dir = recordings_path() / self.camera_id / "motion"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"{capture.event_id}.mp4"
        manifest = staging / "concat.txt"
        files = sorted(staging.glob("part-*.ts"))
        manifest.write_text("ffconcat version 1.0\n" + "".join(
            "file '" + file.as_posix().replace("\\", "\\\\").replace("'", "'\\''") + "'\n" for file in files
        ), encoding="utf-8")
        executable = shutil.which("ffmpeg")
        if not executable or not files:
            await self._mark_failed(capture, "Media encoder unavailable")
            return
        proc = await asyncio.create_subprocess_exec(
            executable, "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(manifest),
            "-map", "0:v:0", "-an", "-c:v", "copy", "-movflags", "+faststart", "-y", str(output),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        if proc.returncode != 0 or not output.is_file() or output.stat().st_size < 128:
            output.unlink(missing_ok=True)
            await self._mark_failed(capture, "Motion clip could not be assembled")
            return
        selected = list(capture.segments.values())
        if not selected:
            await self._mark_failed(capture, "Motion clip has no timestamped video segments")
            output.unlink(missing_ok=True)
            return
        actual_start = min(s.starts_at for s in selected)
        actual_end = max(s.ends_at for s in selected)
        pre_roll_complete = actual_start <= capture.requested_start + timedelta(seconds=self.segment_seconds)
        relative = output.relative_to(recordings_path()).as_posix()
        with connect() as db:
            cursor = db.execute(
                "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                (self.camera_id, relative, actual_start.isoformat(), actual_end.isoformat(), output.stat().st_size),
            )
            segment_id = cursor.lastrowid
            db.execute(
                "UPDATE motion_events SET actual_start=?,actual_end=?,pre_roll_complete=?,status='complete',segment_id=?,failure_reason=NULL WHERE id=?",
                (actual_start.isoformat(), actual_end.isoformat(), int(pre_roll_complete), segment_id, capture.event_id),
            )
        shutil.rmtree(staging, ignore_errors=True)

    async def _mark_failed(self, capture: _Capture, reason: str) -> None:
        with connect() as db:
            db.execute("UPDATE motion_events SET status='failed',failure_reason=? WHERE id=?", (reason, capture.event_id))
        shutil.rmtree(capture.staging_dir, ignore_errors=True)

    async def stop(self) -> None:
        async with self._lock:
            if self.capture is not None:
                await self._mark_failed(self.capture, "Motion recorder stopped before post-roll completed")
                self.capture = None
