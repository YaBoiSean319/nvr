"""Work started in the background is held until it is done.

The event loop keeps only a weak reference to a task. One that nothing else holds can be collected before it has finished: a nudge that is
never sent, or a live stream whose FFmpeg is never ended. Each place that starts such work keeps the task until it completes."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.live_hub import LiveHub
from app.workers import control


class NudgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_nudge_sent_without_waiting_is_kept_until_it_has_been_sent(self):
        release = asyncio.Event()
        sent: list[tuple[str, dict]] = []

        async def nudge(worker: str, message: dict) -> bool:
            await release.wait()
            sent.append((worker, message))
            return True

        with mock.patch.object(control, "nudge", nudge):
            control.RulesClient().nudge()
            control.BackupClient().nudge()
            control.ExportClient().enqueue("job-1")
            self.assertEqual(len(control._nudges), 3)
            release.set()
            await asyncio.gather(*control._nudges)
            await asyncio.sleep(0)
        self.assertEqual(control._nudges, set())
        self.assertEqual(sorted(sent, key=lambda item: item[0]),
                         [("background", {"op": "backup"}), ("exporter", {"op": "enqueue", "job_id": "job-1"}), ("rules", {"op": "evaluate"})])


class LiveHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_stream_being_closed_is_kept_until_it_has_stopped(self):
        release = asyncio.Event()

        class Stream:
            key = ("camera", "main")
            viewers: list[object] = []
            linger = None
            stopped = False

            def remove(self, viewer: object, close) -> None:
                close()  # The last viewer left and the linger is over

            async def stop(self) -> None:
                await release.wait()
                self.stopped = True

        hub, stream = LiveHub(), Stream()
        hub.streams[stream.key] = stream  # type: ignore[assignment]
        hub.leave(stream, object())  # type: ignore[arg-type]
        self.assertNotIn(stream.key, hub.streams)
        self.assertEqual(len(hub._closing), 1)
        release.set()
        await asyncio.gather(*hub._closing)
        await asyncio.sleep(0)
        self.assertTrue(stream.stopped)
        self.assertEqual(hub._closing, set())

    async def test_closing_the_hub_waits_for_a_stream_already_being_closed(self):
        # A stream whose last viewer has left is closed in the background. When the web server stops just after, that close must
        # finish before the event loop does: otherwise its FFmpeg is left running, and is only cleaned up by a later garbage
        # collection on a closed loop (which froze an unrelated test for seconds).
        release = asyncio.Event()

        class Stream:
            key = ("camera", "main")
            viewers: list[object] = []
            linger = None
            stopped = False

            def remove(self, viewer: object, close) -> None:
                close()

            async def stop(self) -> None:
                await release.wait()
                self.stopped = True

        hub, stream = LiveHub(), Stream()
        hub.streams[stream.key] = stream  # type: ignore[assignment]
        hub.leave(stream, object())  # type: ignore[arg-type]
        closing = asyncio.create_task(hub.close())
        await asyncio.sleep(0.05)
        self.assertFalse(closing.done(), "the hub has stopped while a stream was still being closed")
        release.set()
        await asyncio.wait_for(closing, 5)
        self.assertTrue(stream.stopped)


if __name__ == "__main__":
    unittest.main()
