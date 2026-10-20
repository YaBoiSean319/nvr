from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.motion import MotionClipAssembler, parse_live_playlist
from app.onvif_events import CameraMotionEvent, parse_motion_notifications


class MotionTests(unittest.TestCase):
    def test_parse_onvif_motion_transition_and_ignore_non_motion(self):
        xml = b'''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" xmlns:tt="http://www.onvif.org/ver10/schema">
          <wsnt:NotificationMessage><wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
            <wsnt:Message><tt:Message UtcTime="2026-10-20T12:00:00Z"><tt:Data><tt:SimpleItem Name="IsMotion" Value="true"/></tt:Data></tt:Message></wsnt:Message>
          </wsnt:NotificationMessage>
          <wsnt:NotificationMessage><wsnt:Topic>tns1:Device/Temperature</wsnt:Topic>
            <wsnt:Message><tt:Message UtcTime="2026-10-20T12:00:00Z"><tt:Data><tt:SimpleItem Name="State" Value="true"/></tt:Data></tt:Message></wsnt:Message>
          </wsnt:NotificationMessage></s:Envelope>'''
        events = parse_motion_notifications(xml)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].active)
        self.assertEqual(events[0].topic, "tns1:RuleEngine/CellMotionDetector/Motion")
        self.assertEqual(events[0].event_time, datetime(2026, 10, 20, 12, tzinfo=timezone.utc))

    def test_motion_clip_is_persisted_as_playable_video_with_source_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
            os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
            os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
            try:
                from app.store import connect, initialize
                initialize()
                camera_id = str(uuid.uuid4())
                with connect() as db:
                    db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (camera_id, "Motion test", b"encrypted"))
                spool = root / "spool"
                spool.mkdir()
                source = spool / "segment-000001.ts"
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=4:size=64x64:rate=10", "-an", "-c:v", "libx264", "-preset", "ultrafast",
                    "-f", "mpegts", str(source),
                ], check=True, timeout=20)
                start = datetime.now(timezone.utc) - timedelta(seconds=2)
                spool.joinpath("live.m3u8").write_text(
                    "#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:" + start.isoformat() + "\n#EXTINF:4.0,\nsegment-000001.ts\n",
                    encoding="utf-8",
                )
                assembler = MotionClipAssembler(camera_id, spool, pre_roll_seconds=0, post_roll_seconds=1)
                source_time = datetime.now(timezone.utc)
                assembler.ingest(CameraMotionEvent(True, source_time, "RuleEngine/CellMotionDetector/Motion"))
                assembler.ingest(CameraMotionEvent(False, source_time + timedelta(seconds=1), "RuleEngine/CellMotionDetector/Motion"))
                async def finalize():
                    await asyncio.sleep(1.1)
                    await assembler.poll()
                asyncio.run(finalize())
                self.assertIsNone(assembler.capture)
                with connect() as db:
                    event = db.execute("SELECT * FROM motion_events WHERE camera_id=?", (camera_id,)).fetchone()
                    segment = db.execute("SELECT * FROM recording_segments WHERE camera_id=?", (camera_id,)).fetchone()
                self.assertEqual(event["status"], "complete")
                self.assertEqual(event["source_topic"], "RuleEngine/CellMotionDetector/Motion")
                self.assertTrue(event["pre_roll_complete"])
                self.assertIsNotNone(event["actual_start"])
                self.assertIsNotNone(event["actual_end"])
                self.assertEqual(segment["id"], event["segment_id"])
                media = root / "recordings" / segment["relative_path"]
                probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(media)], capture_output=True, text=True, check=True, timeout=10)
                self.assertGreater(float(probe.stdout.strip()), 0)
            finally:
                for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH"):
                    os.environ.pop(key, None)

    def test_live_playlist_rejects_non_local_segment_references(self):
        now = datetime.now(timezone.utc).isoformat()
        playlist = f"#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:{now}\n#EXTINF:2.0,\n../outside.ts\n#EXTINF:2.0,\nsegment-000001.ts\n"
        segments = parse_live_playlist(playlist)
        self.assertEqual([segment.name for segment in segments], ["segment-000001.ts"])


if __name__ == "__main__":
    unittest.main()
