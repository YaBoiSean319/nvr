"""What the supervisor can learn about a worker process and its container: memory in use, the memory limit, and whether the kernel killed something for lack of memory."""
from __future__ import annotations

from pathlib import Path

PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")


def rss_bytes(pid: int, proc: Path = PROC) -> int | None:
    """The resident memory of a process, or None when it cannot be read (it has gone, or this is not Linux)."""
    try:
        for line in (proc / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def memory_limit_bytes(cgroup: Path = CGROUP) -> int | None:
    """The container's memory limit, or None when there is none (or it cannot be read)."""
    for name in ("memory.max", "memory/memory.limit_in_bytes"):
        try:
            text = (cgroup / name).read_text().strip()
        except OSError:
            continue
        if text.isdigit() and int(text) < 1 << 60:  # cgroup v1 reports "no limit" as a huge number, v2 as "max"
            return int(text)
    return None


def oom_kills(cgroup: Path = CGROUP) -> int | None:
    """How many processes the kernel has killed in this container for lack of memory since it started, or None when it cannot be read."""
    for name in ("memory.events", "memory/memory.oom_control"):
        try:
            for line in (cgroup / name).read_text().splitlines():
                key, _, value = line.partition(" ")
                if key == "oom_kill" and value.strip().isdigit():
                    return int(value)
        except OSError:
            continue
    return None


def megabytes(value: int | None) -> int | None:
    return None if value is None else round(value / 1e6)
