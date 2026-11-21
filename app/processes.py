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


async def run_to_end(process: "asyncio.subprocess.Process", input: bytes | None = None, timeout: float | None = None) -> tuple[bytes | None, bytes | None]:
    """``process.communicate(input)`` with a deadline, for a run that gives one answer (a frame, a duration). When the deadline passes, or the
    caller is cancelled, the process is killed and waited for before the error goes on: a cancelled wait does not end it, and an RTSP read
    from a camera that never answers has nothing else to."""
    try:
        return await asyncio.wait_for(process.communicate(input) if input is not None else process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        raise
