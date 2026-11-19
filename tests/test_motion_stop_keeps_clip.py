"""A motion clip in progress is kept when the stream drops or the recorder stops (#339).

Every way out of a motion camera's loop used to mark the event failed and delete the parts already copied: unplug the camera during an event
and the footage up to that moment, pre-roll included, was thrown away."""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import motion
from app.motion import MotionClipAssembler
from app.onvif_events import CameraMotionEvent

TOPIC = "RuleEngine/CellMotionDetector/Motion"
_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")


class StopKeepsClipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.environ["NVR_DB_PATH"] = str(self.root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(self.root / "credentials.key")
        from app.store import connect, initialize

        initialize()
        self.camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (self.camera_id, "Loading dock", b"encrypted"))
        self.spool = self.root / "spool"
        self.spool.mkdir()

    def tearDown(self):
        for key in _ENV:
            os.environ.pop(key, None)
        self.temporary.cleanup()

    def buffer(self, parts: int, ends_in: float = 0.0) -> None:
        """The rolling buffer as FFmpeg leaves it: two-second pieces and a list of them, ending now (or ``ends_in`` seconds from now)."""
        start = datetime.now(timezone.utc) - timedelta(seconds=2 * parts - ends_in)
        lines = ["#EXTM3U"]
        for index in range(parts):
            name = f"segment-{index:06d}.ts"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=64x64:rate=10", "-an", "-c:v", "libx264",
                            "-preset", "ultrafast", "-f", "mpegts", str(self.spool / name)], check=True, timeout=20)
            lines += ["#EXT-X-PROGRAM-DATE-TIME:" + (start + timedelta(seconds=2 * index)).isoformat(), "#EXTINF:2.0,", name]
        (self.spool / "live.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def rows(self):
        from app.store import connect

        with connect() as db:
            return (db.execute("SELECT * FROM motion_events WHERE camera_id=?", (self.camera_id,)).fetchone(),
                    db.execute("SELECT * FROM recording_segments WHERE camera_id=?", (self.camera_id,)).fetchone())

    def test_a_stop_during_an_event_saves_what_was_captured_as_a_playable_clip(self):
        self.buffer(parts=3)
        assembler = MotionClipAssembler(self.camera_id, self.spool, pre_roll_seconds=6, post_roll_seconds=30)

        async def unplugged_mid_event():
            await assembler.ingest(CameraMotionEvent(True, datetime.now(timezone.utc), TOPIC))
            await assembler.poll()  # The event is still going: nothing is finished yet
            self.assertIsNotNone(assembler.capture)
            await assembler.stop()

        asyncio.run(unplugged_mid_event())
        event, segment = self.rows()
        self.assertIsNone(assembler.capture)
        self.assertEqual((event["status"], event["failure_reason"]), ("complete", motion.ENDED_EARLY))
        self.assertIsNotNone(segment, "the footage captured before the stop was thrown away")
        self.assertEqual(event["segment_id"], segment["id"])
        media = self.root / "recordings" / segment["relative_path"]
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(media)], capture_output=True, text=True, check=True, timeout=10)
        self.assertGreater(float(probe.stdout.strip()), 3.0)
        self.assertEqual(list(self.spool.glob("capture-*")), [], "the staging folder was left behind")

    def test_parts_the_buffer_still_holds_are_taken_at_the_stop_even_if_no_pass_had_copied_them(self):
        self.buffer(parts=2)
        assembler = MotionClipAssembler(self.camera_id, self.spool, pre_roll_seconds=4, post_roll_seconds=30)

        async def stopped_at_once():
            await assembler.ingest(CameraMotionEvent(True, datetime.now(timezone.utc), TOPIC))
            await assembler.stop()

        asyncio.run(stopped_at_once())
        event, segment = self.rows()
        self.assertEqual(event["status"], "complete")
        self.assertIsNotNone(segment)

    def test_an_event_with_nothing_captured_is_still_recorded_as_failed(self):
        assembler = MotionClipAssembler(self.camera_id, self.spool, pre_roll_seconds=0, post_roll_seconds=30)  # An empty buffer: no list, no pieces

        async def stopped():
            await assembler.ingest(CameraMotionEvent(True, datetime.now(timezone.utc), TOPIC))
            await assembler.stop()

        asyncio.run(stopped())
        event, segment = self.rows()
        self.assertEqual((event["status"], event["failure_reason"]), ("failed", "Motion recorder stopped before post-roll completed"))
        self.assertIsNone(segment)

    def test_an_event_that_ends_normally_carries_no_note(self):
        self.buffer(parts=3, ends_in=3.0)  # The buffer runs on past the end of the event
        assembler = MotionClipAssembler(self.camera_id, self.spool, pre_roll_seconds=4, post_roll_seconds=1)

        async def whole_event():
            now = datetime.now(timezone.utc)
            await assembler.ingest(CameraMotionEvent(True, now, TOPIC))
            await assembler.ingest(CameraMotionEvent(False, now + timedelta(seconds=1), TOPIC))
            await asyncio.sleep(3.2)
            await assembler.poll()

        asyncio.run(whole_event())
        event, _ = self.rows()
        self.assertEqual((event["status"], event["failure_reason"]), ("complete", None))



class LongEventTests(StopKeepsClipTests):
    """An event that goes on is saved as a series of clips of bounded length (#339)."""

    def everything(self):
        from app.store import connect

        with connect() as db:
            events = db.execute("SELECT * FROM motion_events WHERE camera_id=? ORDER BY received_at", (self.camera_id,)).fetchall()
            segments = {row["id"]: row for row in db.execute("SELECT * FROM recording_segments WHERE camera_id=?", (self.camera_id,))}
        return events, segments

    def more(self, parts: int) -> None:
        """Add pieces to the end of the buffer, as FFmpeg does while the event goes on, leaving the ones already there as they are."""
        listed = (self.spool / "live.m3u8").read_text(encoding="utf-8").rstrip("\n").split("\n")
        have = sum(1 for line in listed if line.endswith(".ts"))
        last_start = datetime.fromisoformat([line for line in listed if line.startswith("#EXT-X-PROGRAM-DATE-TIME:")][-1].split(":", 1)[1])
        for index in range(have, have + parts):
            name = f"segment-{index:06d}.ts"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=64x64:rate=10", "-an", "-c:v", "libx264",
                            "-preset", "ultrafast", "-f", "mpegts", "-y", str(self.spool / name)], check=True, timeout=20)
            listed += ["#EXT-X-PROGRAM-DATE-TIME:" + (last_start + timedelta(seconds=2 * (index - have + 1))).isoformat(), "#EXTINF:2.0,", name]
        (self.spool / "live.m3u8").write_text("\n".join(listed) + "\n", encoding="utf-8")

    def seconds(self, segment) -> float:
        media = self.root / "recordings" / segment["relative_path"]
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(media)], capture_output=True, text=True, check=True, timeout=10)
        return float(probe.stdout.strip())

    def test_a_long_event_is_saved_as_clips_that_follow_on_with_nothing_twice(self):
        self.buffer(parts=3, ends_in=-4.0)  # Three pieces that ended four seconds ago; the two that follow end now
        assembler = MotionClipAssembler(self.camera_id, self.spool, pre_roll_seconds=12, post_roll_seconds=30)

        async def still_going():
            await assembler.ingest(CameraMotionEvent(True, datetime.now(timezone.utc), TOPIC))
            await assembler.poll()  # Six seconds captured, the limit is four: this much is saved and the event carries on
            first = assembler.capture
            self.assertIsNotNone(first)
            self.more(2)  # Two more pieces arrive
            await assembler.poll()
            await assembler.stop()

        with mock.patch.object(motion, "MAX_CLIP_SECONDS", 4):
            asyncio.run(still_going())
        events, segments = self.everything()
        self.assertEqual([(event["status"], event["failure_reason"]) for event in events][0], ("complete", motion.CONTINUES))
        self.assertEqual(len(events), len(segments))
        self.assertGreaterEqual(len(events), 2)
        self.assertTrue(all(event["status"] == "complete" for event in events), [dict(event) for event in events])
        clips = [segments[event["segment_id"]] for event in events]
        self.assertAlmostEqual(self.seconds(clips[0]), 6.0, delta=1.0)
        self.assertAlmostEqual(sum(self.seconds(clip) for clip in clips), 10.0, delta=1.5)  # Five two-second pieces in all, none of them twice
        self.assertTrue(events[1]["pre_roll_complete"])  # A continuation has no pre-roll to be missing
        self.assertEqual(list(self.spool.glob("capture-*")), [])

    def test_an_event_shorter_than_the_limit_is_one_clip_as_before(self):
        self.buffer(parts=3)
        assembler = MotionClipAssembler(self.camera_id, self.spool, pre_roll_seconds=6, post_roll_seconds=30)

        async def short():
            await assembler.ingest(CameraMotionEvent(True, datetime.now(timezone.utc), TOPIC))
            await assembler.poll()
            await assembler.stop()

        asyncio.run(short())
        events, segments = self.everything()
        self.assertEqual((len(events), len(segments), events[0]["failure_reason"]), (1, 1, motion.ENDED_EARLY))


for _name in [name for name in dir(StopKeepsClipTests) if name.startswith("test_")]:
    setattr(LongEventTests, _name, getattr(LongEventTests, _name) if _name in LongEventTests.__dict__ else None)  # The parent's tests are not repeated


if __name__ == "__main__":
    unittest.main()
