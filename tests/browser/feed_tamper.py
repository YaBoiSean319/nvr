"""Feed tamper detection frames to a fixture camera while a journey runs, as the recorder's sampler would."""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone

from tests.browser.seed_fixtures import _camera_id, connect  # noqa: F401 - sets up the import path

from app import tamper  # noqa: E402

WIDTH, HEIGHT = tamper.FRAME_WIDTH, tamper.FRAME_HEIGHT


def scene(shift: int = 0, seed: int = 1) -> bytes:
    """A made-up view with structure: a lit window, a dark door, a fence and a gradient, with a little noise."""
    noise = random.Random(seed)
    rows = []
    for y in range(HEIGHT):
        row = bytearray()
        for x in range(WIDTH):
            u = (x - shift) % WIDTH
            value = 70 + 80 * u // WIDTH + 25 * y // HEIGHT
            if 20 <= u < 60 and 15 <= y < 45:
                value = 215
            elif 90 <= u < 110 and 30 <= y < 85:
                value = 25
            elif y > 60 and (u // 6) % 2 == 0:
                value += 40
            row.append(max(0, min(255, value + noise.randint(-3, 3))))
        rows.append(bytes(row))
    return b"".join(rows)


PATTERNS = {"normal": lambda seed: scene(0, seed), "covered": lambda seed: bytes(8 for _ in range(WIDTH * HEIGHT)), "moved": lambda seed: scene(55, seed)}


def feed(camera: int, pattern: str, count: int, step_seconds: int) -> dict[str, object]:
    camera_id = _camera_id(camera)
    # Dated from now onward, since a rule only fires for moments after it was saved.
    start = datetime.now(timezone.utc)
    state = None
    with connect() as db:
        for index in range(count):
            state = tamper.process_sample(db, camera_id, PATTERNS[pattern](index), start + timedelta(seconds=step_seconds * index))
    return {"camera_id": camera_id, "state": state}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", type=int, required=True)
    parser.add_argument("--pattern", choices=sorted(PATTERNS), required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--step", type=int, default=15)
    options = parser.parse_args()
    print(json.dumps(feed(options.camera, options.pattern, options.count, options.step)))


if __name__ == "__main__":
    main()
