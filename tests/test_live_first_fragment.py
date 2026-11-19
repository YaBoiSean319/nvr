"""The pictures that arrive in the same read as a stream's header are sent, not thrown away.

The stream's header was noticed only after a read's pictures had been gone through, so pictures in the same read as the header were skipped.
A stream that begins with a keyframe lost that keyframe and its viewers waited for the next; a short stream that arrived whole in one read
gave its viewers nothing, which a live-video test hit in CI whenever FFmpeg had finished before its output was first read."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import live_hub


def fragmented_clip() -> bytes:
    """Two seconds of video as the live path's FFmpeg writes it: a header, then picture fragments."""
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "live.mp4"
        subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=160x120:r=10", "-t", "2", "-an",
                        "-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", "10",
                        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-y", str(out)], check=True, timeout=30)
        return out.read_bytes()


class _Pipe:
    def __init__(self, reads: list[bytes]) -> None:
        self.reads = reads

    async def read(self, _size: int) -> bytes:
        await asyncio.sleep(0)
        return self.reads.pop(0) if self.reads else b""


class _Process:
    """Stands in for FFmpeg: hands its output over in the pieces given."""
    returncode = 0

    def __init__(self, reads: list[bytes]) -> None:
        self.stdout = _Pipe(reads)
        self.stdin = mock.Mock()
        self.stdin.drain = mock.AsyncMock()

    def terminate(self) -> None:
        return None

    async def wait(self) -> int:
        return 0


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
class FirstFragmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = fragmented_clip()

    def delivered(self, reads: list[bytes]) -> tuple[bytes | None, list[bytes]]:
        """What one viewer is given when FFmpeg's output arrives in these reads: (the header, the picture fragments)."""
        async def run():
            stream = live_hub.LiveStream(("camera", "main"), "rtsp://camera.invalid/main", False)
            viewer = live_hub.Viewer()
            stream.add(viewer)

            async def spawn(*_args, **_kwargs):
                return _Process(list(reads))

            with mock.patch.object(live_hub.asyncio, "create_subprocess_exec", spawn), mock.patch.object(live_hub.shutil, "which", lambda name: "/usr/bin/ffmpeg"):
                await stream._run()
            items = []
            while not viewer.queue.empty():
                items.append(viewer.queue.get_nowait())
            return stream.init, [item for item in items if item is not None]

        return asyncio.run(run())

    def test_a_stream_that_arrives_whole_in_one_read_still_gives_its_pictures(self):
        header, pictures = self.delivered([self.media])
        self.assertIsNotNone(header)
        self.assertGreaterEqual(len(pictures), 1, "the pictures that came with the header were thrown away")

    def test_the_same_pictures_are_sent_however_the_output_is_cut_into_reads(self):
        whole = self.delivered([self.media])
        for size in (1, 7, 512, 4096):
            pieces = [self.media[offset:offset + size] for offset in range(0, len(self.media), size)]
            self.assertEqual(self.delivered(pieces), whole, f"reads of {size} bytes")
        rest = self.media[len(whole[0]):]
        sent = b"".join(whole[1])
        self.assertTrue(rest.startswith(sent))  # Every picture after the header, in order
        self.assertEqual(rest[len(sent) + 4:len(sent) + 8], b"mfra")  # All that is left out is the file's closing index, which is not video
