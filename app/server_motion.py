from __future__ import annotations

from dataclasses import dataclass


FRAME_WIDTH = 160
FRAME_HEIGHT = 90


@dataclass(frozen=True)
class MotionZone:
    x: float
    y: float
    width: float
    height: float

    @classmethod
    def from_mapping(cls, value: dict[str, float]) -> "MotionZone":
        x, y = float(value["x"]), float(value["y"])
        width, height = float(value["width"]), float(value["height"])
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
            raise ValueError("Motion zones must be positive rectangles within normalized frame bounds")
        return cls(x, y, width, height)

    def pixel_bounds(self, frame_width: int, frame_height: int) -> tuple[int, int, int, int]:
        left = min(frame_width - 1, int(self.x * frame_width))
        top = min(frame_height - 1, int(self.y * frame_height))
        right = min(frame_width, max(left + 1, int((self.x + self.width) * frame_width)))
        bottom = min(frame_height, max(top + 1, int((self.y + self.height) * frame_height)))
        return left, top, right, bottom


class FrameMotionDetector:
    """Bounded pixel-difference motion detector independent of object inference."""

    def __init__(
        self,
        zones: list[dict[str, float]] | None = None,
        sensitivity: int = 50,
        frame_width: int = FRAME_WIDTH,
        frame_height: int = FRAME_HEIGHT,
        trigger_frames: int = 2,
        clear_frames: int = 3,
    ) -> None:
        if not 1 <= int(sensitivity) <= 100:
            raise ValueError("Motion sensitivity must be between 1 and 100")
        self.zones = [MotionZone.from_mapping(zone) for zone in (zones or [])]
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.threshold = 2.0 + (100 - int(sensitivity)) * 0.08
        self.trigger_frames = max(1, trigger_frames)
        self.clear_frames = max(1, clear_frames)
        self.previous: bytes | None = None
        self.active = False
        self._motion_count = 0
        self._quiet_count = 0

    def _average_difference(self, frame: bytes) -> float:
        bounds = [zone.pixel_bounds(self.frame_width, self.frame_height) for zone in self.zones]
        if not bounds:
            bounds = [(0, 0, self.frame_width, self.frame_height)]
        total = 0
        count = 0
        stride = self.frame_width
        assert self.previous is not None
        for left, top, right, bottom in bounds:
            for y in range(top, bottom):
                offset = y * stride
                for x in range(left, right):
                    total += abs(frame[offset + x] - self.previous[offset + x])
                    count += 1
        return total / count if count else 0

    def process(self, frame: bytes) -> bool | None:
        expected = self.frame_width * self.frame_height
        if len(frame) != expected:
            raise ValueError(f"Expected a {expected}-byte grayscale frame")
        if self.previous is None:
            self.previous = frame
            return None
        moving = self._average_difference(frame) >= self.threshold
        self.previous = frame
        if moving:
            self._motion_count += 1
            self._quiet_count = 0
            if not self.active and self._motion_count >= self.trigger_frames:
                self.active = True
                self._motion_count = 0
                return True
        else:
            self._quiet_count += 1
            self._motion_count = 0
            if self.active and self._quiet_count >= self.clear_frames:
                self.active = False
                self._quiet_count = 0
                return False
        return None
