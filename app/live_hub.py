"""Shared live streams: one camera connection, many viewers.

FFmpeg copies the camera's H.264 video (and, when the administrator enabled it,
AAC audio) into fragmented MP4 without re-encoding the picture. A ``LiveStream``
splits that output into an initialization segment and media fragments and fans
them out to every viewer of the same camera profile. A viewer that joins late
starts at the most recent keyframe fragment, so it decodes immediately. The
camera connection closes a few seconds after the last viewer leaves.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator

from .recording import _PROTOCOLS, _manifest_bytes

logger = logging.getLogger("nvr.live")

LINGER_SECONDS = 5.0
VIEWER_QUEUE_FRAGMENTS = 120
READ_TIMEOUT_SECONDS = 15.0
MAX_BOX_BYTES = 32 * 1024 * 1024
NON_SYNC_SAMPLE = 0x00010000


# --- fragmented MP4 parsing -------------------------------------------------------

def _boxes(data: bytes | memoryview, start: int = 0, end: int | None = None) -> Iterator[tuple[str, int, int, int]]:
    end = len(data) if end is None else end
    offset = start
    while offset + 8 <= end:
        size, kind = struct.unpack(">I4s", bytes(data[offset:offset + 8]))
        header = 8
        if size == 1:
            size = struct.unpack(">Q", bytes(data[offset + 8:offset + 16]))[0]
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            return
        yield kind.decode("latin-1"), offset, size, header
        offset += size


def _child(data: bytes, parent: tuple[int, int, int], kind: str) -> tuple[int, int, int] | None:
    offset, size, header = parent
    for name, child_offset, child_size, child_header in _boxes(data, offset + header, offset + size):
        if name == kind:
            return child_offset, child_size, child_header
    return None


def codec_string(init: bytes) -> str:
    """The MSE codecs parameter for an initialization segment."""
    codecs = []
    marker = init.find(b"avcC")
    if marker >= 0 and marker + 8 <= len(init):
        profile, compatibility, level = init[marker + 5], init[marker + 6], init[marker + 7]
        codecs.append(f"avc1.{profile:02x}{compatibility:02x}{level:02x}")
    if init.find(b"mp4a") >= 0:
        codecs.append("mp4a.40.2")
    return f'video/mp4; codecs="{", ".join(codecs)}"'


def _default_sample_flags(init: bytes, track_id: int) -> int | None:
    for name, offset, size, header in _boxes(init):
        if name != "moov":
            continue
        mvex = _child(init, (offset, size, header), "mvex")
        if mvex is None:
            return None
        for child, child_offset, child_size, child_header in _boxes(init, mvex[0] + mvex[2], mvex[0] + mvex[1]):
            if child == "trex":
                body = child_offset + child_header + 4
                if struct.unpack(">I", init[body:body + 4])[0] == track_id:
                    return struct.unpack(">I", init[body + 16:body + 20])[0]
    return None


def starts_with_keyframe(moof: bytes, init: bytes) -> bool:
    """Whether a fragment's first video sample is a sync sample (decodable on its own)."""
    root = (0, len(moof), 8)
    traf = _child(moof, root, "traf")
    if traf is None:
        return False
    tfhd, trun = _child(moof, traf, "tfhd"), _child(moof, traf, "trun")
    if tfhd is None or trun is None:
        return False
    body = tfhd[0] + tfhd[2]
    flags = struct.unpack(">I", moof[body:body + 4])[0] & 0xFFFFFF
    track_id = struct.unpack(">I", moof[body + 4:body + 8])[0]
    cursor = body + 8
    default_flags = None
    for present, width in ((0x1, 8), (0x2, 4), (0x8, 4), (0x10, 4)):
        if flags & present:
            cursor += width
    if flags & 0x20:
        default_flags = struct.unpack(">I", moof[cursor:cursor + 4])[0]
    if default_flags is None:
        default_flags = _default_sample_flags(init, track_id)
    body = trun[0] + trun[2]
    run_flags = struct.unpack(">I", moof[body:body + 4])[0] & 0xFFFFFF
    cursor = body + 8 + (4 if run_flags & 0x1 else 0)
    if run_flags & 0x4:
        sample_flags = struct.unpack(">I", moof[cursor:cursor + 4])[0]
    elif run_flags & 0x400:
        cursor += 4
        cursor += 4 if run_flags & 0x100 else 0
        cursor += 4 if run_flags & 0x200 else 0
        sample_flags = struct.unpack(">I", moof[cursor:cursor + 4])[0]
    else:
        sample_flags = default_flags
    return sample_flags is not None and not sample_flags & NON_SYNC_SAMPLE


@dataclass
class Fragment:
    data: bytes
    keyframe: bool


class Fragmenter:
    """Incrementally split a fragmented MP4 byte stream into init and fragments."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.init: bytes | None = None
        self._pending_init = bytearray()
        self._pending_moof: bytes | None = None

    def feed(self, chunk: bytes) -> list[Fragment]:
        self.buffer.extend(chunk)
        fragments: list[Fragment] = []
        while len(self.buffer) >= 8:
            size, kind = struct.unpack(">I4s", bytes(self.buffer[:8]))
            if size == 1:
                if len(self.buffer) < 16:
                    break
                size = struct.unpack(">Q", bytes(self.buffer[8:16]))[0]
            if size < 8 or size > MAX_BOX_BYTES:
                raise ValueError("Live stream produced an invalid MP4 box")
            if len(self.buffer) < size:
                break
            box = bytes(self.buffer[:size])
            del self.buffer[:size]
            name = kind.decode("latin-1")
            if self.init is None:
                self._pending_init.extend(box)
                if name == "moov":
                    self.init = bytes(self._pending_init)
                continue
            if name == "moof":
                self._pending_moof = box
            elif name == "mdat" and self._pending_moof is not None:
                fragments.append(Fragment(self._pending_moof + box, starts_with_keyframe(self._pending_moof, self.init)))
                self._pending_moof = None
        return fragments


# --- shared streams ---------------------------------------------------------------

class ViewerTooSlow(Exception):
    pass


@dataclass(eq=False)
class Viewer:
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(VIEWER_QUEUE_FRAGMENTS))
    closed_reason: str | None = None

    def offer(self, item: object) -> None:
        if self.closed_reason:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            # Never let one slow browser hold the camera's shared stream back.
            self.closed_reason = "too_slow"
            self.queue = asyncio.Queue(1)
            self.queue.put_nowait(None)


class LiveStream:
    def __init__(self, key: tuple[str, ...], source: str, include_audio: bool,
                 on_fragment: Callable[[], None] | None = None, video_filter: str | None = None) -> None:
        self.key = key
        # A privacy mask: the video is transcoded with it drawn in, instead of passed through.
        self.video_filter = video_filter
        self.source = source
        self.include_audio = include_audio
        self.on_fragment = on_fragment
        self.viewers: set[Viewer] = set()
        self.init: bytes | None = None
        self.mime: str | None = None
        self.gop: list[bytes] = []
        self.ready = asyncio.Event()
        self.failed: str | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task[None] | None = None
        self.linger: asyncio.TimerHandle | None = None
        self.connections = 0

    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name=f"live-{'-'.join(self.key)}")

    async def stop(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def add(self, viewer: Viewer) -> None:
        if self.linger:
            self.linger.cancel()
            self.linger = None
        self.viewers.add(viewer)
        # Start the newcomer at the latest keyframe so its first frame decodes at once.
        for fragment in self.gop:
            viewer.offer(fragment)

    def remove(self, viewer: Viewer, on_idle: Callable[[], None]) -> None:
        self.viewers.discard(viewer)
        if not self.viewers and self.linger is None:
            self.linger = asyncio.get_running_loop().call_later(LINGER_SECONDS, on_idle)

    def _broadcast(self, item: object) -> None:
        for viewer in list(self.viewers):
            viewer.offer(item)

    async def _run(self) -> None:
        executable = shutil.which("ffmpeg")
        try:
            if not executable:
                raise RuntimeError("Live video is unavailable because FFmpeg is missing")
            args = [
                executable, "-hide_banner", "-nostats", "-loglevel", "error", "-fflags", "+genpts",
                "-f", "concat", "-safe", "0", "-protocol_whitelist", _PROTOCOLS, "-i", "pipe:0",
                "-map", "0:v:0",
            ]
            if self.video_filter:
                args += ["-vf", self.video_filter, "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                         "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", "30"]
            else:
                args += ["-c:v", "copy"]
            if self.include_audio:
                args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k", "-ac", "1"]
            else:
                args += ["-an"]
            args += ["-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "300000", "pipe:1"]
            self.process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            self.connections += 1
            assert self.process.stdin and self.process.stdout
            self.process.stdin.write(_manifest_bytes(self.source))
            await self.process.stdin.drain()
            self.process.stdin.close()
            fragmenter = Fragmenter()
            while True:
                chunk = await asyncio.wait_for(self.process.stdout.read(65536), READ_TIMEOUT_SECONDS)
                if not chunk:
                    raise RuntimeError("The camera stream ended")
                for fragment in fragmenter.feed(chunk):
                    if self.init is None:
                        continue
                    if fragment.keyframe:
                        self.gop = [fragment.data]
                    elif self.gop:
                        self.gop.append(fragment.data)
                    else:
                        continue  # nothing decodable yet
                    self._broadcast(fragment.data)
                    if self.on_fragment:
                        self.on_fragment()
                if self.init is None and fragmenter.init is not None:
                    self.init = fragmenter.init
                    self.mime = codec_string(self.init)
                    self.ready.set()
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.failed = "No video arrived from the camera for 15 seconds"
        except (OSError, RuntimeError, ValueError) as exc:
            self.failed = str(exc) or "The camera stream failed"
        finally:
            if self.process and self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            if self.failed:
                self.ready.set()
                self._broadcast(None)


class LiveHub:
    """Registry of shared streams, keyed by camera, profile and audio."""

    def __init__(self) -> None:
        self.streams: dict[tuple[str, ...], LiveStream] = {}

    def join(self, key: tuple[str, ...], source: str, include_audio: bool, viewer: Viewer,
             on_fragment: Callable[[], None] | None = None, video_filter: str | None = None) -> LiveStream:
        stream = self.streams.get(key)
        if stream is None or stream.failed:
            stream = LiveStream(key, source, include_audio, on_fragment, video_filter)
            self.streams[key] = stream
            stream.start()
        stream.add(viewer)
        return stream

    def leave(self, stream: LiveStream, viewer: Viewer) -> None:
        def close_if_idle() -> None:
            stream.linger = None
            if not stream.viewers and self.streams.get(stream.key) is stream:
                self.streams.pop(stream.key, None)
                asyncio.create_task(stream.stop())

        stream.remove(viewer, close_if_idle)

    async def close(self) -> None:
        streams = list(self.streams.values())
        self.streams.clear()
        await asyncio.gather(*(stream.stop() for stream in streams), return_exceptions=True)

    def snapshot(self) -> list[dict[str, object]]:
        return [{"key": list(stream.key), "viewers": len(stream.viewers), "camera_connections": stream.connections,
                 "ready": stream.init is not None, "failed": stream.failed} for stream in self.streams.values()]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
