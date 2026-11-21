"""Ending a child process (FFmpeg) the one way: ask it to stop, give it a grace period, then kill it, and wait for it in every case (#387).

The waiting matters as much as the killing: a child that has ended but was not waited for stays in the process table, and one that was
never killed keeps its camera's stream open. A whole worker that is killed takes its children with it through its process group (#322);
this is for the children a running worker ends itself.
"""
from __future__ import annotations

import asyncio


async def end_process(process: "asyncio.subprocess.Process | None", grace: float) -> None:
    """Ask ``process`` to stop, wait up to ``grace`` seconds for it, then kill it and wait for it. Nothing to do when there is no process or it
    has already ended. A process that ends between the check and the signal is not an error."""
    if process is None or process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
