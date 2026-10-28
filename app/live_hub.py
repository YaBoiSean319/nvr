"""Shared live streams: one camera connection, many viewers.

FFmpeg copies the camera's H.264 (or, for a browser that can play it, H.265) video (and, when the administrator enabled it,
AAC audio) into fragmented MP4 without re-encoding the picture. A browser that cannot play H.265 is given the same camera converted to H.264
instead (``transcode``), once for all of its viewers, on a graphics chip when there is one (``acceleration``). A ``LiveStream``
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

from . import acceleration
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


def hevc_codec(init: bytes) -> str | None:
    """The RFC 6381 codec string (``hvc1.1.6.L93.B0``) of an H.265 track from its ``hvcC`` box, or None when there is none."""
    marker = init.find(b"hvcC")
    if marker < 0 or marker + 4 + 13 > len(init):
        return None
    record = init[marker + 4:marker + 4 + 13]
    space, tier, profile = record[1] >> 6, (record[1] >> 5) & 1, record[1] & 0x1F
    compatibility = int(f"{struct.unpack('>I', record[2:6])[0]:032b}"[::-1], 2)  # The flags are written in reverse bit order
    constraints = list(record[6:12])
    while constraints and constraints[-1] == 0:
        constraints.pop()
    tag = "hvc1" if init.find(b"hvc1") >= 0 else "hev1"
    parts = [f"{tag}.{'' if space == 0 else 'ABC'[space - 1]}{profile}", f"{compatibility:x}", f"{'L' if tier == 0 else 'H'}{record[12]}", *(f"{value:02X}" for value in constraints)]
    return ".".join(parts)


def codec_string(init: bytes) -> str:
    """The MSE codecs parameter for an initialization segment."""
    codecs = []
    marker = init.find(b"avcC")
    if marker >= 0 and marker + 8 <= len(init):
        profile, compatibility, level = init[marker + 5], init[marker + 6], init[marker + 7]
        codecs.append(f"avc1.{profile:02x}{compatibility:02x}{level:02x}")
    elif (hevc := hevc_codec(init)) is not None:
        codecs.append(hevc)
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
                 on_fragment: Callable[[], None] | None = None, video_filter: str | None = None,
                 transcode: bool = False, hevc_copy: bool = False) -> None:
        self.key = key
        # A privacy mask: the video is transcoded with it drawn in, instead of passed through.
        self.video_filter = video_filter
        # Converted to H.264 for a browser that cannot play the camera's H.265; or passed through as H.265 (tagged so Safari plays it).
        self.transcode, self.hevc_copy = transcode, hevc_copy
        self.accelerated = False
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

    @property
    def converts(self) -> bool:
        """Whether the picture is re-encoded (masked, or H.265 converted) rather than copied."""
        return self.video_filter is not None or self.transcode

    def _arguments(self, executable: str, hardware: bool) -> list[str]:
        args = [executable, "-hide_banner", "-nostats", "-loglevel", "error", "-fflags", "+genpts"]
        encoder = acceleration.encoder(crf=26, speed="ultrafast") if hardware else acceleration.build(None, crf=26, speed="ultrafast")
        if self.converts:
            args += [*encoder.pre, *(acceleration.decode_args() if hardware else [])]
        args += ["-f", "concat", "-safe", "0", "-protocol_whitelist", _PROTOCOLS, "-i", "pipe:0", "-map", "0:v:0"]
        if self.converts:
            chain = encoder.filters(self.video_filter)
            if chain:
                args += ["-vf", chain]
            if encoder.name is None:
                args += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", "30"]
            else:
                args += [*encoder.codec, "-g", "30"]
        else:
            args += ["-c:v", "copy"] + (["-tag:v", "hvc1"] if self.hevc_copy else [])
        if self.include_audio:
            args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k", "-ac", "1"]
        else:
            args += ["-an"]
        return args + ["-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "300000", "pipe:1"]

    async def _run(self) -> None:
        executable = shutil.which("ffmpeg")
        try:
            if not executable:
                raise RuntimeError("Live video is unavailable because FFmpeg is missing")
            hardware = self.converts and acceleration.selected() is not None
            try:
                await self._session(executable, hardware)
            except (OSError, RuntimeError, asyncio.TimeoutError, ValueError) as exc:
                if not (hardware and self.init is None):
                    raise
                # The device did not work for this stream: say so, stop using it, and do the same work in software.
                acceleration.disable(str(exc) or "the hardware encoder failed to start")
                await self._session(executable, False)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.failed = "No video arrived from the camera for 15 seconds"
        except (OSError, RuntimeError, ValueError) as exc:
            self.failed = str(exc) or "The camera stream failed"
        finally:
            if self.failed:
                self.ready.set()
                self._broadcast(None)

    async def _session(self, executable: str, hardware: bool) -> None:
        self.accelerated = hardware
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self._arguments(executable, hardware), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
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
        finally:
            if self.process and self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()


class LiveHub:
    """Registry of shared streams, keyed by camera, profile and audio."""

    def __init__(self) -> None:
        self.streams: dict[tuple[str, ...], LiveStream] = {}

    def join(self, key: tuple[str, ...], source: str, include_audio: bool, viewer: Viewer,
             on_fragment: Callable[[], None] | None = None, video_filter: str | None = None,
             transcode: bool = False, hevc_copy: bool = False) -> LiveStream:
        stream = self.streams.get(key)
        if stream is None or stream.failed:
            stream = LiveStream(key, source, include_audio, on_fragment, video_filter, transcode, hevc_copy)
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
                 "ready": stream.init is not None, "failed": stream.failed, "transcoded": stream.transcode,
                 "accelerated": stream.accelerated} for stream in self.streams.values()]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
