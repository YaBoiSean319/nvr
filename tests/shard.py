"""Split the unit test modules into balanced shards, so CI can run them on several runners at once.

Each runner has its own machine, so no test shares a processor with another shard: the timing-sensitive tests (the event-loop stall checks,
the stream and process tests) see the same conditions as in one long run. ``tests/module_seconds.json`` holds how long each module took; a
module that is not listed (a new one) counts as the median. Modules are given out longest first, each to the shard with the least time so
far, so the shards end at about the same time; the split depends only on the module list and the timings, so every runner computes the same one.

    python -m tests.shard --shard 1 --of 4          # the modules of shard 1, one per line
    python -m tests.shard --shard 1 --of 4 --run    # run them (python -m unittest -v, with the tests package imported first)
    python -m tests.shard --check 4                 # every module is in exactly one of the 4 shards
    python -m tests.shard --from-log job-logs.txt   # write module_seconds.json from a CI unit-test log (the preferred source)
    python -m tests.shard --measure                 # or time every module here (tests that need CI's camera or store are skipped)

The timings in the repository came from a CI log: GitHub stamps every log line with its time and the tests run with -v, so the time between
one test's line and the next is that test's. Measured locally, the tests that need CI's simulated camera or object store are skipped and look
cheap. Regenerate them when the shards drift apart (the "Every test module is in exactly one shard" step prints each shard's estimate).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
TIMINGS = TESTS / "module_seconds.json"


def modules() -> list[str]:
    """Every module ``python -m unittest discover -s tests -t .`` would load."""
    return sorted(f"tests.{path.stem}" for path in TESTS.glob("test*.py"))


def timings() -> dict[str, float]:
    try:
        return {name: float(seconds) for name, seconds in json.loads(TIMINGS.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError):
        return {}


def shards(count: int, names: list[str] | None = None, seconds: dict[str, float] | None = None) -> list[list[str]]:
    names = modules() if names is None else names
    seconds = timings() if seconds is None else seconds
    default = statistics.median(seconds.values()) if seconds else 1.0
    cost = {name: seconds.get(name, default) for name in names}
    loads = [0.0] * count
    split: list[list[str]] = [[] for _ in range(count)]
    for name in sorted(names, key=lambda item: (-cost[item], item)):
        target = min(range(count), key=lambda index: (loads[index], index))
        split[target].append(name)
        loads[target] += cost[name]
    return [sorted(part) for part in split]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.shard")
    parser.add_argument("--shard", type=int, help="which shard, from 1")
    parser.add_argument("--of", type=int, help="how many shards")
    parser.add_argument("--run", action="store_true", help="run the shard's modules")
    parser.add_argument("--check", type=int, metavar="COUNT", help="check that COUNT shards hold every module exactly once")
    parser.add_argument("--measure", action="store_true", help="time every module and write module_seconds.json")
    parser.add_argument("--from-log", type=Path, metavar="LOG", help="write module_seconds.json from CI unit-test logs (one or several shards)", nargs="+")
    arguments = parser.parse_args(argv)
    if arguments.measure:
        return measure()
    if arguments.from_log:
        return from_logs(arguments.from_log)
    if arguments.check:
        split = shards(arguments.check)
        given = [name for part in split for name in part]
        if sorted(given) != modules() or len(given) != len(set(given)):
            print("The shards do not hold every test module exactly once", file=sys.stderr)
            return 1
        estimate = timings()
        for index, part in enumerate(split, 1):
            print(f"shard {index}: {len(part)} modules, about {sum(estimate.get(name, 0.0) for name in part) / 60:.1f} min")
        return 0
    if not arguments.shard or not arguments.of or not 1 <= arguments.shard <= arguments.of:
        parser.error("give --shard N --of COUNT, with 1 <= N <= COUNT")
    chosen = shards(arguments.of)[arguments.shard - 1]
    if not arguments.run:
        print("\n".join(chosen))
        return 0
    # Importing tests.<module> imports the tests package first, as "discover -t ." does (see tests/__init__.py).
    return subprocess.call([sys.executable, "-m", "unittest", "-v", *chosen])


_LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{1,6})\d*Z .*\((tests\.test_\w+)\.\w+\.\w+\)")


def seconds_from_log(text: str) -> dict[str, float]:
    """Each module's time from one CI log: the time from one test's line to the next is given to the next test's module."""
    from datetime import datetime

    marks = [(datetime.fromisoformat(match.group(1)), match.group(2)) for match in map(_LOG_LINE.match, text.splitlines()) if match]
    seconds: dict[str, float] = {}
    for (before, _), (after, name) in zip(marks, marks[1:]):
        seconds[name] = seconds.get(name, 0.0) + (after - before).total_seconds()
    if marks:
        seconds.setdefault(marks[0][1], 0.0)
    return seconds


def from_logs(logs: list[Path]) -> int:
    seconds: dict[str, float] = {}
    for log in logs:
        seconds.update(seconds_from_log(log.read_text(encoding="utf-8", errors="replace")))
    if not seconds:
        print("No test lines found: give the log of a CI unit-test job", file=sys.stderr)
        return 1
    TIMINGS.write_text(json.dumps({name: round(value, 1) for name, value in seconds.items()}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(seconds)} modules, {sum(seconds.values()) / 60:.1f} min in all")
    return 0


def measure() -> int:
    """Run every module in one process, as the whole suite runs, and write how long each took."""
    import tests  # noqa: F401 - first, as in a real run

    loader, seconds, failed = unittest.TestLoader(), {}, False
    for name in modules():
        started = time.monotonic()
        result = unittest.TextTestRunner(stream=open("/dev/null", "w"), verbosity=0).run(loader.loadTestsFromName(name))
        seconds[name] = round(time.monotonic() - started, 1)
        failed = failed or not result.wasSuccessful()
        print(f"{seconds[name]:7.1f}s {name}{'' if result.wasSuccessful() else '  (failures)'}", flush=True)
    TIMINGS.write_text(json.dumps(seconds, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
