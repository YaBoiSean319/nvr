"""ONVIF media profiles as the NVR needs them: codec, size and frame rate, and which to record and which to watch.

Media1 and Media2 describe a profile differently (``trt:Profiles`` with ``tt:VideoEncoderConfiguration``; ``tr2:Profiles`` with
``tr2:Configurations/tr2:VideoEncoder``). Both are read into one ``MediaProfile``. ``choose_profiles`` then picks:

- **the recording profile**: the best codec the camera offers, H.264 first (it copies straight into the recordings and plays everywhere),
  then H.265, then MJPEG, which is only used when nothing else exists (its files are many times larger and a camera often lists it first);
  within a codec, the most pixels, then the highest frame rate, then the highest bit rate;
- **the live profile**: an H.264 profile with fewer pixels than the recording profile, for the grid: the largest at or under 1280x720, else
  the smallest there is. None when the camera has no such profile (live then uses the recording stream).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

CODEC_RANK = {"h264": 0, "h265": 1, "mjpeg": 2, "other": 3}
LIVE_PIXEL_CAP = 1280 * 720


@dataclass(frozen=True)
class MediaProfile:
    token: str
    name: str
    encoding: str = "other"  # h264, h265, mjpeg or other
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bitrate_kbps: int = 0
    has_audio: bool = False
    has_ptz: bool = False
    source: str = "media1"  # which service described it: media1 or media2

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def view(self) -> dict[str, object]:
        return {"token": self.token, "name": self.name, "encoding": self.encoding, "width": self.width, "height": self.height,
                "fps": self.fps, "bitrate_kbps": self.bitrate_kbps, "has_audio": self.has_audio, "has_ptz": self.has_ptz, "source": self.source}


@dataclass(frozen=True)
class Selection:
    record: MediaProfile | None
    live: MediaProfile | None
    reason: str
    problems: tuple[str, ...] = field(default_factory=tuple)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalise_encoding(value: str | None) -> str:
    text = (value or "").strip().casefold().replace(".", "").replace("-", "").replace("_", "")
    if text in {"h264", "avc", "avc1"}:
        return "h264"
    if text in {"h265", "hevc"}:
        return "h265"
    if text in {"jpeg", "mjpeg", "motionjpeg"}:
        return "mjpeg"
    return "other"


def _text(node: ET.Element, name: str) -> str | None:
    found = next((child for child in node.iter() if _local(child.tag) == name and child.text and child.text.strip()), None)
    return found.text.strip() if found is not None else None


def _number(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def _encoder_of(profile: ET.Element) -> ET.Element | None:
    return next((node for node in profile.iter() if _local(node.tag) in {"VideoEncoderConfiguration", "VideoEncoder"} and any(_local(child.tag) == "Encoding" for child in node)), None)


def parse_profiles(root: ET.Element, source: str) -> list[MediaProfile]:
    """The profiles in a Media1 (``source="media1"``) or Media2 (``"media2"``) GetProfiles response."""
    profiles: list[MediaProfile] = []
    for node in root.iter():
        if _local(node.tag) != "Profiles" or not node.attrib.get("token"):
            continue
        encoder = _encoder_of(node)
        encoding, width, height, fps, bitrate = "other", 0, 0, 0.0, 0
        if encoder is not None:
            encoding = normalise_encoding(_text(encoder, "Encoding"))
            width, height = int(_number(_text(encoder, "Width"))), int(_number(_text(encoder, "Height")))
            fps = _number(_text(encoder, "FrameRateLimit"))
            bitrate = int(_number(_text(encoder, "BitrateLimit")))
        names = {_local(child.tag) for child in node.iter()}
        profiles.append(MediaProfile(
            token=node.attrib["token"], name=(_text(node, "Name") or node.attrib["token"])[:128], encoding=encoding, width=width, height=height, fps=fps,
            bitrate_kbps=bitrate, has_audio=bool(names & {"AudioEncoderConfiguration", "AudioEncoder"}), has_ptz=bool(names & {"PTZConfiguration", "PTZ"}), source=source))
    return profiles


def _best(profiles: list[MediaProfile]) -> MediaProfile:
    return max(profiles, key=lambda item: (item.pixels, item.fps, item.bitrate_kbps))


def choose_profiles(profiles: list[MediaProfile]) -> Selection:
    """Which profile to record and which to show in the grid; see the module docstring for the policy."""
    if not profiles:
        return Selection(None, None, "The camera listed no media profiles", ("No compatible video profile found",))
    usable = [item for item in profiles if item.encoding != "other"]
    if not usable:
        return Selection(None, None, "None of the camera's profiles use a video codec the NVR can record", ("No compatible video profile found",))
    best_rank = min(CODEC_RANK[item.encoding] for item in usable)
    record = _best([item for item in usable if CODEC_RANK[item.encoding] == best_rank])
    others = {"h264": "H.264", "h265": "H.265", "mjpeg": "MJPEG"}
    reason = f"Recording {record.name} ({others[record.encoding]}, {record.width}x{record.height})"
    skipped = sorted({others[item.encoding] for item in usable if CODEC_RANK[item.encoding] > best_rank})
    if skipped:
        reason += f"; {' and '.join(skipped)} profiles were not chosen because {others[record.encoding]} is available"
    problems = ("Only MJPEG is available: recordings will be much larger and playback may be limited",) if record.encoding == "mjpeg" else ()
    smaller = [item for item in usable if item.encoding == "h264" and item.token != record.token and 0 < item.pixels < record.pixels]
    live = None
    if smaller:
        within = [item for item in smaller if item.pixels <= LIVE_PIXEL_CAP]
        live = _best(within) if within else min(smaller, key=lambda item: (item.pixels, -item.fps))
    return Selection(record, live, reason, problems)
