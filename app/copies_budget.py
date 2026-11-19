"""One disk budget for what the NVR makes from footage: exports, case packages and playback copies (#362).

All three are written to the recordings volume, and the recorder deletes the oldest footage when free space falls below the volume's
headroom. Without a limit, making exports one after another, or playing converted footage, pushed recordings out a gigabyte at a time,
even for an operator who may not delete footage. The owner's decision:

- together they may use ``BUDGET_SHARE`` (10%) of the recordings volume;
- at the limit, playback copies (masked or converted for the browser, which can be made again) are removed first, oldest first;
- if that is not enough, a new export or case package is refused with a message that says why. Nobody's export is deleted to make room.

A playback copy is never refused: making one removes older copies, so copies cannot pile up past the budget.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

BUDGET_SHARE = 0.10
KINDS = ("exports", "case_packages", "playback_copies")


class BudgetFull(RuntimeError):
    """A new export or case package does not fit in the budget even after the playback copies were removed."""


def _directories() -> dict[str, Path]:
    from .cases import PACKAGE_DIRECTORY
    from .exports import EXPORT_DIRECTORY
    from .privacy import MASKED_COPY_DIRECTORY
    from .storage import recordings_path

    root = recordings_path()
    return {"exports": root / EXPORT_DIRECTORY, "case_packages": root / PACKAGE_DIRECTORY, "playback_copies": root / MASKED_COPY_DIRECTORY}


def _size(directory: Path) -> int:
    total, stack = 0, [directory]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    return total


def usage() -> dict[str, int]:
    """Bytes used by each of the three, as files on disk now (an export being written counts as it grows)."""
    return {kind: _size(directory) for kind, directory in _directories().items()}


def budget_bytes() -> int:
    from .storage import recordings_path

    try:
        return int(shutil.disk_usage(recordings_path()).total * BUDGET_SHARE)
    except OSError:
        return 0


def room_left() -> int:
    """How many more bytes the three may use before the budget is reached (never below zero)."""
    return max(0, budget_bytes() - sum(usage().values()))


def _playback_copies_oldest_first() -> list[Path]:
    found = []
    for path in _directories()["playback_copies"].glob("*.mp4"):
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            pass
    return [path for _, path in sorted(found)]


def make_room(needed: int, *, refuse: bool = True, keep: Path | None = None) -> None:
    """Make ``needed`` more bytes fit: remove playback copies, oldest first, until they do. If they still do not and ``refuse``, raise
    ``BudgetFull``; a playback copy (``refuse=False``) is made anyway once the older copies are gone. ``keep`` is a copy not to remove."""
    budget = budget_bytes()
    used = sum(usage().values())
    if used + needed <= budget:
        return
    for copy in _playback_copies_oldest_first():
        if copy == keep:
            continue
        try:
            size = copy.stat().st_size
            copy.unlink()
        except OSError:
            continue
        used -= size
        if used + needed <= budget:
            return
    if refuse:
        raise BudgetFull(
            f"Exports and case packages already use their share of the recordings disk ({_gib(budget)} GB, 10%). "
            "Remove exports or case packages that are no longer needed, or wait for exports to expire, then try again.")


def _gib(value: int) -> str:
    return f"{value / (1 << 30):.1f}"


def view() -> dict[str, object]:
    used = usage()
    return {"budget_bytes": budget_bytes(), "share": BUDGET_SHARE, "used_bytes": used, "total_used_bytes": sum(used.values())}
