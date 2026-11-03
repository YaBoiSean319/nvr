"""How many cameras the AI worker analyses (#238): not a fixed number but what the machine can carry.

Every analysed camera costs CPU (its own FFmpeg process decodes the camera's whole stream; the analysis rate only picks which frames are kept), time in the one inference slot all
cameras share, and memory. The ``Controller`` looks at those every few seconds and moves a limit: it adds one camera at a time only after the machine has had clear headroom for
a while, takes cameras away at once when CPU, the inference slot, memory or lateness pass a limit, then holds off growing and remembers the count that failed, so it does not
oscillate. ``Sampler`` reads the measurements from the container (cgroup limits when there are some, otherwise the machine). Both are plain code over numbers, tested without a
real machine.

An administrator can still choose a fixed number instead (``capacity_mode`` 'fixed' in the runtime settings).
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")

EVALUATE_SECONDS = 15.0
GROW_AFTER = 4  # Quiet evaluations in a row (a minute) before one more camera is added
HOLDOFF_SECONDS = 600.0  # After taking cameras away, no growing for this long
SETTLE_SECONDS = 30.0  # After taking cameras away, no more taken for this long (the measurements need time to show the effect)
AVOID_SECONDS = 3600.0  # A count that failed is not tried again for this long
START_CAMERAS = 4
RESUME_SHARE = 0.75  # After a restart, resume at this share of the last limit and grow back
CPU_MAX, CPU_GROW = 0.85, 0.65  # Share of the cores the container may use
INFERENCE_MAX, INFERENCE_GROW = 0.85, 0.65  # Share of the time the one inference slot is busy
MEMORY_MAX, MEMORY_GROW = 0.85, 0.70  # Share of the memory the container may use (not counting file cache)
MIN_FREE_BYTES = 1 << 30
GROW_FREE_BYTES = 2 << 30
LATE_MAX = 0.25  # Share of the analysed cameras that were delayed waiting for inference
LATE_SECONDS = 60.0  # How long a camera counts as late after a delayed frame
PER_CAMERA_BYTES = 200 << 20  # Only for the ceiling of the search, never for a decision: measured memory decides
CAMERAS_PER_CORE = 8


@dataclass
class Sample:
    """What was measured over the last interval. ``None`` means it could not be read, and is then not used."""
    cpu: float | None = None
    inference_busy: float = 0.0
    memory_used: float | None = None
    memory_free: int | None = None
    late_share: float = 0.0


@dataclass
class Decision:
    limit: int
    action: str  # start, hold, grow, shed
    factor: str | None  # What is closest to its limit: cpu, inference, memory or late
    detail: str = ""


def hardware_ceiling(cores: float, memory_bytes: int | None) -> int:
    """The most cameras worth trying on this machine: a bound for the search, not a promise (the measurements decide)."""
    by_cores = int(max(1.0, cores) * CAMERAS_PER_CORE)
    by_memory = int(0.6 * memory_bytes / PER_CAMERA_BYTES) if memory_bytes else by_cores
    return max(1, min(by_cores, by_memory))


def pressure(sample: Sample) -> list[tuple[str, float]]:
    """The loads as shares of their limits (1.0 is at the limit), for deciding and for saying what is closest."""
    loads: list[tuple[str, float]] = []
    if sample.cpu is not None:
        loads.append(("cpu", sample.cpu / CPU_MAX))
    loads.append(("inference", sample.inference_busy / INFERENCE_MAX))
    if sample.memory_used is not None:
        loads.append(("memory", sample.memory_used / MEMORY_MAX))
    if sample.memory_free is not None:
        loads.append(("memory", MIN_FREE_BYTES / max(1, sample.memory_free)))
    loads.append(("late", sample.late_share / LATE_MAX))
    return loads


@dataclass
class Controller:
    ceiling: int
    limit: int = START_CAMERAS
    streak: int = 0
    holdoff_until: float = 0.0
    settle_until: float = 0.0
    avoid_from: int | None = None
    avoid_until: float = 0.0
    last: Decision | None = field(default=None, repr=False)

    @classmethod
    def starting(cls, ceiling: int, last_limit: int | None = None) -> "Controller":
        """A controller for a worker that has just started: where it left off (a little lower, in case that was too many), or a few cameras."""
        start = max(1, int(last_limit * RESUME_SHARE)) if last_limit else START_CAMERAS
        return cls(ceiling=ceiling, limit=max(1, min(start, ceiling)))

    def evaluate(self, sample: Sample, now: float, analysed: int, demand: int) -> Decision:
        loads = pressure(sample)
        closest = max(loads, key=lambda item: item[1])[0] if loads else None
        over = [name for name, load in loads if load > 1.0]
        if over:
            self.streak = 0
            if now < self.settle_until and not (sample.memory_used or 0) > 0.92:
                self.last = Decision(self.limit, "hold", over[0], "settling after taking cameras away")
                return self.last
            severe = (sample.cpu or 0) > 0.95 or (sample.memory_used or 0) > 0.92 or (sample.memory_free is not None and sample.memory_free < MIN_FREE_BYTES // 2)
            basis = max(1, analysed)
            step = max(1, math.ceil(basis * (0.2 if severe else 0.1)))
            self.avoid_from, self.avoid_until = basis, now + AVOID_SECONDS
            self.limit = max(1, basis - step)
            self.holdoff_until, self.settle_until = now + HOLDOFF_SECONDS, now + SETTLE_SECONDS
            self.last = Decision(self.limit, "shed", over[0], f"{over[0]} passed its limit at {basis} cameras")
            return self.last
        quiet = ((sample.cpu is None or sample.cpu < CPU_GROW) and sample.inference_busy < INFERENCE_GROW
                 and (sample.memory_used is None or sample.memory_used < MEMORY_GROW) and (sample.memory_free is None or sample.memory_free >= GROW_FREE_BYTES) and sample.late_share == 0)
        wants_more = demand > self.limit and analysed >= self.limit  # Cameras are waiting, and every camera allowed is running
        if quiet and wants_more:
            self.streak += 1
        else:
            self.streak = 0
        candidate = self.limit + 1
        blocked = self.avoid_from is not None and now < self.avoid_until and candidate >= self.avoid_from
        if self.streak >= GROW_AFTER and now >= self.holdoff_until and not blocked and candidate <= self.ceiling:
            self.limit, self.streak = candidate, 0
            self.last = Decision(self.limit, "grow", closest, "the machine has had room for a while")
            return self.last
        if demand > self.limit and candidate > self.ceiling:
            why = "the most this machine is expected to carry"
        elif blocked:
            why = f"{self.avoid_from} cameras was too many a little while ago"
        elif now < self.holdoff_until:
            why = "waiting a while after taking cameras away"
        elif wants_more:
            why = "waiting to be sure there is room"
        else:
            why = ""
        self.last = Decision(self.limit, "hold", closest, why)
        return self.last


class Sampler:
    """Reads CPU, memory and the cores available from the container (cgroup v2 when it has limits) or the machine. Paths can be changed for tests."""

    def __init__(self, proc: Path = PROC, cgroup: Path = CGROUP, clock=time.monotonic) -> None:
        self.proc, self.cgroup, self.clock = proc, cgroup, clock
        self._cpu_at: tuple[float, float] | None = None  # (monotonic, cumulative CPU seconds)

    def _read(self, path: Path) -> str | None:
        try:
            return path.read_text()
        except OSError:
            return None

    def cores(self) -> float:
        text = self._read(self.cgroup / "cpu.max")
        if text:
            quota, _, period = text.strip().partition(" ")
            if quota.isdigit() and period.isdigit() and int(period) > 0:
                return max(0.1, int(quota) / int(period))
        try:
            return float(len(os.sched_getaffinity(0)))
        except (AttributeError, OSError):
            return float(os.cpu_count() or 1)

    def _cpu_seconds(self) -> float | None:
        text = self._read(self.cgroup / "cpu.stat")
        if text:
            for line in text.splitlines():
                key, _, value = line.partition(" ")
                if key == "usage_usec" and value.strip().isdigit():
                    return int(value) / 1e6
        text = self._read(self.proc / "stat")
        if text and text.startswith("cpu "):
            fields = [int(item) for item in text.splitlines()[0].split()[1:] if item.isdigit()]
            if len(fields) >= 4:
                busy = sum(fields) - fields[3] - (fields[4] if len(fields) > 4 else 0)  # Not idle, not waiting for the disk
                return busy / (os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100)
        return None

    def cpu(self) -> float | None:
        """The share of the available cores in use since the last call (None the first time, or when it cannot be read)."""
        now, total = self.clock(), self._cpu_seconds()
        previous, self._cpu_at = self._cpu_at, (now, total) if total is not None else None
        if previous is None or total is None or now <= previous[0]:
            return None
        return max(0.0, min(1.0, (total - previous[1]) / ((now - previous[0]) * self.cores())))

    def memory(self) -> tuple[float | None, int | None, int | None]:
        """(share of the memory in use, bytes free, bytes the container may use): from the container's limit when it has one, else the machine's. File cache is not counted as in use."""
        limit = self._read(self.cgroup / "memory.max")
        current = self._read(self.cgroup / "memory.current")
        if limit and limit.strip().isdigit() and current and current.strip().isdigit():
            total, used = int(limit), int(current)
            stat = self._read(self.cgroup / "memory.stat") or ""
            cache = next((int(line.split()[1]) for line in stat.splitlines() if line.startswith("inactive_file ")), 0)
            used = max(0, used - cache)
            return used / total, max(0, total - used), total
        text = self._read(self.proc / "meminfo")
        if text:
            values = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in text.splitlines() if ":" in line and line.split()[1:2] and line.split()[1].isdigit()}
            total, free = values.get("MemTotal"), values.get("MemAvailable")
            if total and free is not None:
                return (total - free) / total, free, total
        return None, None, None

    def memory_total(self) -> int | None:
        return self.memory()[2]


class Capacity:
    """The AI worker's capacity state: what the cameras' analysis loops report, the controller, and the status written for the settings page."""

    def __init__(self, sampler: Sampler | None = None, clock=time.monotonic) -> None:
        self.sampler = sampler or Sampler()
        self.clock = clock
        self.controller: Controller | None = None
        self.busy_seconds = 0.0
        self._busy_mark = (clock(), 0.0)
        self.late: dict[str, float] = {}
        self.evaluated_at = float("-inf")
        self.last_sample = Sample()

    def note_inference(self, seconds: float) -> None:
        self.busy_seconds += seconds

    def note_late(self, camera_id: str) -> None:
        self.late[camera_id] = self.clock() + LATE_SECONDS

    def forget(self, camera_id: str) -> None:
        self.late.pop(camera_id, None)

    def sample(self, analysed: int) -> Sample:
        now = self.clock()
        mark_at, mark_busy = self._busy_mark
        busy = min(1.0, (self.busy_seconds - mark_busy) / (now - mark_at)) if now > mark_at else 0.0
        self._busy_mark = (now, self.busy_seconds)
        self.late = {camera: until for camera, until in self.late.items() if until > now}
        used, free, _ = self.sampler.memory()
        self.last_sample = Sample(cpu=self.sampler.cpu(), inference_busy=busy, memory_used=used, memory_free=free, late_share=(len(self.late) / analysed) if analysed else 0.0)
        return self.last_sample

    def due(self) -> bool:
        return self.clock() - self.evaluated_at >= EVALUATE_SECONDS

    def evaluate(self, analysed: int, demand: int, last_limit: int | None = None) -> Decision:
        """Measure and decide (call about every ``EVALUATE_SECONDS``). The controller is made on the first call, resuming near ``last_limit`` if there was one."""
        now = self.clock()
        if self.controller is None:
            self.controller = Controller.starting(hardware_ceiling(self.sampler.cores(), self.sampler.memory_total()), last_limit)
            self.sampler.cpu()  # The first reading only sets the mark
            self.sample(analysed)
            self.evaluated_at = now
            decision = Decision(self.controller.limit, "start", None, "starting carefully")
            self.controller.last = decision
            return decision
        self.evaluated_at = now
        return self.controller.evaluate(self.sample(analysed), now, analysed, demand)

    def status(self, mode: str, limit: int, analysed: int, wanting: int) -> dict[str, Any]:
        sample, controller = self.last_sample, self.controller
        total = self.sampler.memory_total()
        return {
            "mode": mode, "limit_cameras": limit, "analysed": analysed, "wanting": wanting,
            "factor": controller.last.factor if controller and controller.last else None, "action": controller.last.action if controller and controller.last else None,
            "detail": controller.last.detail if controller and controller.last else "",
            "cpu": sample.cpu, "cores": self.sampler.cores(), "inference_busy": sample.inference_busy,
            "memory_used_bytes": int(sample.memory_used * total) if sample.memory_used is not None and total else None, "memory_total_bytes": total, "late_share": sample.late_share,
        }
