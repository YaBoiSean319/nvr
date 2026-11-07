"""Which live stream a viewer is given (#216): the profile, how it is prepared for the browser, and the key viewers of the same stream share.

The live stream endpoint and the keeper that holds streams ready before anyone asks (``app/live_ready.py``) both use this, so a stream kept ready is exactly the one
a viewer then joins."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import privacy

HEVC_NAMES = {"hevc", "h265", "hev1", "hvc1"}
H264_NAMES = {"h264", "avc", "avc1"}


@dataclass(frozen=True)
class Variant:
    video_filter: str | None  # A privacy mask: the picture is converted (H.264) with the mask drawn in
    profile: str  # "sub" or "main"
    codec: str
    camera_is_hevc: bool
    hevc_copy: bool  # An H.265 camera passed on as H.265, for a browser that plays it
    transcode: bool  # An H.265 camera converted to H.264 for one that does not
    include_audio: bool

    @property
    def mode(self) -> str:
        return "masked" if self.video_filter else "h264" if self.transcode else "hevc" if self.hevc_copy else "direct"

    @property
    def playable_directly(self) -> bool:
        """False for a main stream whose codec is neither H.264 nor H.265 and is not converted by a mask: the browser has to use the compatibility stream."""
        return bool(self.video_filter) or self.profile != "main" or not self.codec or self.codec in H264_NAMES or self.camera_is_hevc


def choose(camera: Mapping[str, Any], zones: Sequence[Any], quality: str, hevc: bool) -> Variant:
    """The variant for a camera row (``live_substream_url_ciphertext``, ``video_codec``, ``audio_enabled``), the masks that apply, the asked quality and
    whether the browser plays H.265."""
    video_filter = privacy.drawbox_filter(list(zones)) if zones else None
    profile = "sub" if quality == "low" and camera["live_substream_url_ciphertext"] else "main"
    codec = (camera["video_codec"] or "").lower()
    camera_is_hevc = codec in HEVC_NAMES
    hevc_copy = camera_is_hevc and hevc and profile == "main" and not video_filter
    # A masked picture is converted anyway (to H.264). The low-quality stream's codec is not recorded, so for an H.265 camera it is always converted unless passed on as H.265.
    transcode = camera_is_hevc and not hevc_copy and not video_filter
    return Variant(video_filter, profile, codec, camera_is_hevc, hevc_copy, transcode, bool(camera["audio_enabled"]))


def key_of(camera_id: str, variant: Variant, source: str) -> tuple[str, ...]:
    """What viewers of the same stream share. The source fingerprint keeps a changed stream URL (rotation, new profile) from joining a stream still connected to the old one."""
    fingerprint = hashlib.sha256(f"{source}|{variant.video_filter or ''}".encode()).hexdigest()[:16]
    return (camera_id, variant.profile, "audio" if variant.include_audio else "video", variant.mode, fingerprint)
