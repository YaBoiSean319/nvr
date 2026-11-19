from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import metrics
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
    source: str = ""
    topic: str = ""
    skip: set[str] = field(default_factory=set)  # Buffer parts the clip before this one already holds


STOP_JOIN_SECONDS = 20.0  # While stopping, a clip gets this long to be joined: inside the recorder's stop grace
ENDED_EARLY = "The recording stopped before the event ended; the clip holds what was captured until then"
# An event that goes on (a door left open, a camera whose motion never clears) is saved as a series of clips of at most this length.
# One clip for the whole event kept every part in the motion buffer until the end, and lost all of it if it could not be joined (#339).
MAX_CLIP_SECONDS = 600
CONTINUES = "A long event: it continues in the next clip"


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
                    source=event.source, topic=event.topic,
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
        now = datetime.now(timezone.utc)
        if not self._copy_buffered(capture, now):
            return
        if capture.copied and (capture.active or now < capture.capture_until) and self._length(capture) >= MAX_CLIP_SECONDS:
            # Still going, and long enough: this much is saved now and the event carries on in a new clip from where this one ends.
            self.capture = self._continuation(capture, now)
            await self._finalize(capture, note=CONTINUES)
            return
        if capture.active or now < capture.capture_until or not capture.copied:
            return
        if capture.last_segment_end and capture.last_segment_end < capture.capture_until:
            return
        await self._finalize(capture)
        self.capture = None

    @staticmethod
    def _length(capture: _Capture) -> float:
        starts = [segment.starts_at for segment in capture.segments.values()]
        return (capture.last_segment_end - min(starts)).total_seconds() if starts and capture.last_segment_end else 0.0

    def _continuation(self, capture: _Capture, now: datetime) -> _Capture:
        """The next clip of an event that is still going: it starts where the last one ends, with no pre-roll of its own."""
        following = _Capture(
            event_id=str(uuid.uuid4()), first_received_at=now, requested_start=capture.last_segment_end or now,
            first_source_event_at=capture.first_source_event_at, capture_until=capture.capture_until, active=capture.active,
            source_events=list(capture.source_events[-8:]), staging_dir=self.spool_dir / ("capture-" + uuid.uuid4().hex),
            source=capture.source, topic=capture.topic, skip=capture.skip | capture.copied,
        )
        following.staging_dir.mkdir(parents=True, exist_ok=True)
        with connect() as db:
            db.execute(
                "INSERT INTO motion_events(id,camera_id,source,source_topic,source_event_at,received_at,requested_start,requested_end,pre_roll_complete,status,source_events_json) "
                "VALUES(?,?,?,?,?,?,?,?,0,'recording',?)",
                (following.event_id, self.camera_id, following.source, following.topic, following.first_source_event_at.isoformat(), now.isoformat(),
                 following.requested_start.isoformat(), following.capture_until.isoformat(), json.dumps(following.source_events, separators=(",", ":"))),
            )
        return following

    def _copy_buffered(self, capture: _Capture, now: datetime) -> bool:
        """Copy the buffer's segments that belong to this capture and are not yet copied into its staging folder. False when the buffer's list cannot be read."""
        try:
            segments = parse_live_playlist((self.spool_dir / "live.m3u8").read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return False
        for segment in (s for s in segments if s.ends_at >= capture.requested_start and s.starts_at <= now):
            if segment.name in capture.copied or segment.name in capture.skip:
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
        return True

    async def _finalize(self, capture: _Capture, *, timeout: float | None = None, note: str | None = None) -> None:
        from .recording import recordings_path
        from .storage import camera_recordings_dir
        staging = capture.staging_dir
        output_dir = camera_recordings_dir(self.camera_id) / "motion"
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
        with connect() as db:
            camera = db.execute("SELECT audio_enabled FROM cameras WHERE id=?", (self.camera_id,)).fetchone()
        from .recording import audio_recording_args
        audio_args = audio_recording_args(bool(camera and camera["audio_enabled"]))
        proc = await asyncio.create_subprocess_exec(
            executable, "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(manifest),
            "-map", "0:v:0", *audio_args, "-c:v", "copy", "-movflags", "+faststart", "-y", str(output),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            # Joining is a copy, and takes longer for a longer clip: two minutes, or half a second for each two-second part if that is more (#339).
            await asyncio.wait_for(proc.wait(), timeout=timeout if timeout is not None else max(120.0, len(files) * 0.5))
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
            try:
                metrics.segment_written(db, self.camera_id, actual_end)
            except sqlite3.Error:
                pass  # A counter must not strand the clip it counts.
            db.execute(
                "UPDATE motion_events SET actual_start=?,actual_end=?,pre_roll_complete=?,status='complete',segment_id=?,failure_reason=? WHERE id=?",
                (actual_start.isoformat(), actual_end.isoformat(), int(pre_roll_complete), segment_id, note, capture.event_id),
            )
            db.execute("UPDATE cameras SET last_footage_write_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),recording_fault_since=NULL WHERE id=?", (self.camera_id,))
        shutil.rmtree(staging, ignore_errors=True)

    async def _mark_failed(self, capture: _Capture, reason: str) -> None:
        metrics.segment_dropped(self.camera_id)
        with connect() as db:
            db.execute("UPDATE motion_events SET status='failed',failure_reason=? WHERE id=?", (reason, capture.event_id))
        shutil.rmtree(capture.staging_dir, ignore_errors=True)

    async def stop(self) -> None:
        """End the capture in progress, keeping what it holds. The camera was unplugged, the stream dropped or the recorder is stopping: the
        footage up to that moment, pre-roll included, is the footage that matters, and it used to be thrown away (#339). What the buffer still
        has is copied, the parts are joined into a clip, and the event is complete with a note that it ended early."""
        async with self._lock:
            capture, self.capture = self.capture, None
            if capture is None:
                return
            try:
                self._copy_buffered(capture, datetime.now(timezone.utc))
                if capture.copied:
                    await self._finalize(capture, timeout=STOP_JOIN_SECONDS, note=ENDED_EARLY)
                    return
                if capture.skip:
                    # The next clip of a long event, begun a moment ago with nothing in it yet: the clips before it hold the event, and
                    # an empty one is not a recording that failed.
                    with connect() as db:
                        db.execute("DELETE FROM motion_events WHERE id=? AND status='recording'", (capture.event_id,))
                    shutil.rmtree(capture.staging_dir, ignore_errors=True)
                    return
            except (OSError, sqlite3.Error):
                pass
            await self._mark_failed(capture, "Motion recorder stopped before post-roll completed")
