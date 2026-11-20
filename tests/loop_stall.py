"""How long an event loop stops turning while another connection holds the database's write lock.

A worker's event loop looks after all its cameras and sends the heartbeat the supervisor watches; a database write made on it waits for the
lock, and everything on the loop waits with it (three long waits in a row reach the supervisor's 30-second kill). These helpers hold the lock
from another connection, run some work on a fresh event loop, and return the longest the loop went without turning."""
from __future__ import annotations

import asyncio
import gc
import sqlite3
import threading
import time
from typing import Any, Awaitable, Callable

HOLD_SECONDS = 1.0


def longest_stall_while_locked(work: Callable[[], Awaitable[Any]], hold_seconds: float = HOLD_SECONDS) -> float:
    """Run ``work()`` while another connection holds the write lock for ``hold_seconds``; return the longest the event loop went without
    turning.

    The garbage collector is run first and kept off while this is measured, as in tests/test_loop_stays_free.py: a collection of what other
    tests left behind pauses the whole process, not the loop alone. Anything time-sensitive the work reads (a file that must be fresh) is to
    be made inside ``work``, after that collection."""
    from app.store import database_path

    gc.collect()
    gc.disable()
    try:
        taken, release = threading.Event(), threading.Event()

        def hold() -> None:
            other = sqlite3.connect(database_path(), timeout=5, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                taken.set()
                release.wait(hold_seconds)
                other.execute("ROLLBACK")
            finally:
                other.close()

        async def body() -> float:
            longest, running, last = 0.0, True, time.monotonic()

            async def tick() -> None:
                nonlocal longest, last
                while running:
                    await asyncio.sleep(0.01)
                    now = time.monotonic()
                    longest, last = max(longest, now - last), now

            ticker = asyncio.create_task(tick())
            holder = threading.Thread(target=hold)
            holder.start()
            if not await asyncio.to_thread(taken.wait, 5):
                raise AssertionError("the write lock could not be taken")
            try:
                await asyncio.wait_for(work(), timeout=20)
                # Counted here too, before the loop turns again: when the work ends with a stall, the ticker may not get to measure it.
                longest = max(longest, time.monotonic() - last)
            finally:
                release.set()
                await asyncio.to_thread(holder.join)
                running = False
                await ticker
            return longest

        return asyncio.run(body())
    finally:
        gc.enable()
