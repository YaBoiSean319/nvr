"""The one place a picture held in memory becomes a JPEG (#314): FFmpeg reads the raw pixels and writes the file.

Licence-plate crops, face pictures and the face-finding frame differ only in how they are scaled, how hard they are compressed and how long they may take."""
from __future__ import annotations

import shutil
import subprocess

import numpy as np


def encode(image: np.ndarray | None, *, scale: str, quality: int, timeout: float) -> bytes | None:
    """A BGR picture as a JPEG, or None when it is empty, FFmpeg is missing or slow, or the output is not a JPEG.

    ``scale`` is an FFmpeg scale filter, ``quality`` its ``-q:v`` (lower is better), ``timeout`` in seconds."""
    ffmpeg = shutil.which("ffmpeg")
    if image is None or image.ndim != 3 or image.size == 0 or not ffmpeg:
        return None
    height, width = image.shape[:2]
    try:
        result = subprocess.run([ffmpeg, "-v", "error", "-threads", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-i", "pipe:0",
                                 "-vf", scale, "-frames:v", "1", "-q:v", str(quality), "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                                input=np.ascontiguousarray(image, dtype=np.uint8).tobytes(), capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None
    return result.stdout if result.returncode == 0 and result.stdout.startswith(b"\xff\xd8") else None
