"""How the fall decision copes with a noisy pose model: falls it finds and alarms it raises on ordinary movement, by scenario, noise level and sensitivity.

``python -m tests.fixtures.fall_accuracy`` prints the table in docs/fall-detection.md and docs/acceptance/roadmap.md. It feeds the real ``FallDetector`` with made-up body angles (a
torso leaning from upright to lying) with random error added and frames where the person cannot be made out. It measures the decision logic only: **it is not the accuracy of the pose
model on real people or of any camera**, which needs staged falls on the real cameras (see ``python -m app.fall_eval``). Deterministic: fixed seeds.
"""
from __future__ import annotations

import random
from collections.abc import Callable

from app import falls
from tests.test_falls import pose

STEP = 1.0  # Seconds between analysed frames (one frame a second)
TRIALS = 200
SECONDS = 60


def _ramp(start: float, end: float, frames: int) -> list[float]:
    return [start + (end - start) * (index + 1) / frames for index in range(frames)]


def _scenario(lead: int, *parts: list[float]) -> list[float]:
    return [3.0] * lead + [value for part in parts for value in part]


# name -> (is a fall, the true torso angle at each frame)
SCENARIOS: dict[str, tuple[bool, Callable[[], list[float]]]] = {
    "Falls and stays down": (True, lambda: _scenario(20, _ramp(3, 85, 2), [85.0] * 38)),
    "Falls in one frame and stays down": (True, lambda: _scenario(20, _ramp(3, 80, 1), [85.0] * 39)),
    "Stumbles and gets up within 2 s": (False, lambda: _scenario(20, [70.0, 75.0], _ramp(75, 3, 2), [3.0] * 36)),
    "Sits down on a chair": (False, lambda: _scenario(20, _ramp(3, 25, 4), [25.0] * 36)),
    "Bends to pick something up": (False, lambda: _scenario(20, _ramp(3, 60, 3), [60.0] * 3, _ramp(60, 3, 3), [3.0] * 31)),
    "Crouches to tie a shoe": (False, lambda: _scenario(20, _ramp(3, 42, 2), [42.0] * 6, _ramp(42, 3, 2), [3.0] * 30)),
    "Lies down slowly on a bed (12 s)": (False, lambda: _scenario(20, _ramp(3, 85, 12), [85.0] * 28)),
    "Was already lying (in bed) when first seen": (False, lambda: [85.0] * SECONDS),
    "Walks about": (False, lambda: [3.0] * SECONDS),
}

NOISE = (0.0, 5.0, 10.0, 15.0)  # Degrees of random error on every angle
DROPOUT = (0.0, 0.2)  # Share of frames in which the person cannot be made out


def trial(angles: list[float], sensitivity: str, stay_down: float, noise: float, dropout: float, rng: random.Random) -> int:
    detector = falls.FallDetector(sensitivity=sensitivity, stay_down_seconds=stay_down)
    raised = 0
    for index, angle in enumerate(angles):
        now = index * STEP
        if rng.random() < dropout:
            raised += len(detector.tick(now, set()))
            continue
        shown = max(0.0, min(90.0, angle + rng.gauss(0, noise)))
        raised += 1 if detector.update("person", now, pose(shown)) else 0
    return raised


def measure(sensitivity: str, stay_down: float, noise: float, dropout: float, seed: int = 11) -> list[tuple[str, bool, float, float]]:
    """(scenario, is a fall, share of trials in which at least one fall was raised, mean falls raised per trial)."""
    rows = []
    for name, (is_fall, build) in SCENARIOS.items():
        rng = random.Random(f"{seed}-{name}-{sensitivity}-{stay_down}-{noise}-{dropout}")
        counts = [trial(build(), sensitivity, stay_down, noise, dropout, rng) for _ in range(TRIALS)]
        rows.append((name, is_fall, sum(1 for item in counts if item) / TRIALS, sum(counts) / TRIALS))
    return rows


# (sensitivity, seconds a person must stay down)
SETTINGS = (("high", 3.0), ("high", 8.0), ("normal", 3.0), ("low", 3.0))
CONDITIONS = ((0.0, 0.0), (10.0, 0.0), (10.0, 0.2))  # (degrees of error, share of frames unseen)


def table() -> str:
    lines = [f"Fall decision on made-up body angles ({TRIALS} trials per cell, one analysed frame a second)", ""]
    header = "| Scenario | " + " | ".join(f"{noise:g}° error{', ' + str(int(dropout * 100)) + '% unseen' if dropout else ''}" for noise, dropout in CONDITIONS) + " |"
    for sensitivity, stay_down in SETTINGS:
        lines += [f"Sensitivity {sensitivity}, stay down {stay_down:g} s", header, "| --- | " + " | ".join("---" for _ in CONDITIONS) + " |"]
        cells = {condition: measure(sensitivity, stay_down, *condition) for condition in CONDITIONS}
        for index, (name, (is_fall, _)) in enumerate(SCENARIOS.items()):
            lines.append(f"| {name}{' (a fall)' if is_fall else ''} | " + " | ".join(f"{cells[condition][index][2]:.0%}" for condition in CONDITIONS) + " |")
        lines.append("")
    lines.append("Each cell is the share of trials in which an alert was raised: high for the fall rows is good, low for the others is good.")
    return "\n".join(lines)


if __name__ == "__main__":
    print(table())
