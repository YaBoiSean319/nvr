"""Edge backfill (#131): an outage is filled from the camera's own recording over ONVIF Profile G."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import backfill, coverage
from app.credentials import encrypt
from app.main import app
from app.principal import current_user
from app.store import connect, initialize
from tests.fixtures.onvif_camera import OnvifCamera
from tests.fixtures.onvif_recordings import RecordingCamera, make_footage
from tests.test_support import principal

FFMPEG = shutil.which("ffmpeg")
OUTAGE = 12  # Seconds the NVR missed
NOW = datetime.now(timezone.utc).replace(microsecond=0)
START = NOW - timedelta(minutes=30)  # The live footage before the outage ends here
RESUMED = START + timedelta(seconds=OUTAGE)  # and live footage resumes here


@unittest.skipUnless(FFMPEG, "FFmpeg is required to fetch and play the backfilled footage")
class BackfillCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        # The camera recorded from 10 s before the NVR lost it to 20 s after it came back.
        cls.footage = make_footage(Path(cls.folder.name) / "card.mp4", seconds=OUTAGE + 30)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = {"NVR_DB_PATH": str(Path(self.temporary.name) / "state.sqlite3"), "NVR_RECORDINGS_PATH": str(Path(self.temporary.name) / "recordings"),
                            "NVR_CREDENTIAL_KEY_PATH": str(Path(self.temporary.name) / "credentials.key")}
        self.previous = {key: os.environ.get(key) for key in self.environment}
        os.environ.update(self.environment)
        (Path(self.temporary.name) / "recordings").mkdir()
        os.environ.pop("NVR_STORAGE_VOLUMES", None)
        initialize()
        self.camera = RecordingCamera(footage=self.footage, earliest=START - timedelta(seconds=10), seconds=OUTAGE + 30)
        self.camera_id = self.add_camera(self.camera)
        self.patches = [mock.patch.object(backfill, "FIRST_CHUNK", timedelta(seconds=3)), mock.patch.object(backfill, "CHUNK", timedelta(seconds=4)),
                        mock.patch.object(backfill, "MIN_GAP", timedelta(seconds=5))]
        for patch in self.patches:
            patch.start()
        self.user = ("admin", "administrator")
        app.dependency_overrides[current_user] = lambda: principal(1, *self.user)
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        app.dependency_overrides.pop(current_user, None)
        for patch in self.patches:
            patch.stop()
        self.camera.close()
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def add_camera(self, device: OnvifCamera, enabled: bool = True) -> str:
        camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,device_service_url,motion_credentials_ciphertext,backfill_enabled) VALUES(?,?,X'01','continuous',?,?,?)",
                       (camera_id, f"Loading dock {camera_id[:6]}", device.url, encrypt(json.dumps({"username": device.username, "password": device.password})), int(enabled)))
        with connect() as db:
            db.execute("UPDATE cameras SET created_at=? WHERE id=?", ((START - timedelta(seconds=60)).isoformat(), camera_id))
        self.live_segment(camera_id, START - timedelta(seconds=60), 60)
        self.live_segment(camera_id, RESUMED, int((NOW - RESUMED).total_seconds()))  # Recording again from then until now
        return camera_id

    def live_segment(self, camera_id: str, starts: datetime, seconds: int) -> None:
        path = Path(self.temporary.name) / "recordings" / camera_id / f"segment-{starts:%Y%m%dT%H%M%SZ}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"live footage" * 20)
        with connect() as db:
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                       (camera_id, f"{camera_id}/{path.name}", starts.isoformat(), (starts + timedelta(seconds=seconds)).isoformat(), path.stat().st_size))

    def run_backfill(self, **options) -> int:
        return asyncio.run(backfill.BackfillManager().tick(NOW, **options))

    def jobs(self, camera_id: str | None = None) -> list[dict]:
        with connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM backfill_jobs WHERE camera_id=? ORDER BY starts_at", (camera_id or self.camera_id,))]

    def backfilled(self, camera_id: str | None = None) -> list[dict]:
        with connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM recording_segments WHERE camera_id=? AND source='backfill' ORDER BY starts_at", (camera_id or self.camera_id,))]

    def timeline(self) -> dict:
        with connect() as db:
            return coverage.timeline(db, self.camera_id, START - timedelta(seconds=60), START + timedelta(minutes=2), limit=None)


class FillTests(BackfillCase):
    def test_a_simulated_outage_is_filled_from_the_cameras_recording_and_plays_back(self):
        self.assertEqual([gap["reason_code"] for gap in self.timeline()["gaps"]], ["unknown"])
        self.run_backfill()
        job = self.jobs()[0]
        self.assertEqual((job["state"], job["starts_at"], job["ends_at"]), ("done", START.isoformat(), RESUMED.isoformat()))
        self.assertAlmostEqual(job["filled_seconds"], OUTAGE, delta=1.5)
        segments = self.backfilled()
        self.assertGreaterEqual(len(segments), 3)  # 3 s, 4 s, 4 s chunks
        self.assertEqual(segments[0]["starts_at"], START.isoformat())
        for before, after in zip(segments, segments[1:]):
            self.assertAlmostEqual((datetime.fromisoformat(after["starts_at"]) - datetime.fromisoformat(before["ends_at"])).total_seconds(), 0, delta=1.5)
        self.assertLess(sum((datetime.fromisoformat(gap["ends_at"]) - datetime.fromisoformat(gap["starts_at"])).total_seconds() for gap in self.timeline()["gaps"]), 2.0)
        # Played through the same endpoint as any footage: the video is the camera's, copied rather than re-encoded.
        response = self.client.get(f"/api/cameras/{self.camera_id}/segments/{segments[0]['id']}/media")
        self.assertEqual(response.status_code, 200, response.text)
        path = Path(self.temporary.name) / "recordings" / segments[0]["relative_path"]
        self.assertEqual(response.content, path.read_bytes())
        probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height", "-of", "json", str(path)],
                                          capture_output=True, check=True).stdout)["streams"][0]
        self.assertEqual((probe["codec_name"], probe["width"], probe["height"]), ("h264", 640, 360))
        self.assertTrue(path.name.startswith("backfill-"))

    def test_backfilled_footage_is_marked_on_the_timeline_and_in_the_coverage_api(self):
        self.run_backfill()
        timeline = self.client.post("/api/playback/timeline", json={"camera_ids": [self.camera_id], "start": (START - timedelta(minutes=2)).isoformat(),
                                                                    "end": (START + timedelta(minutes=2)).isoformat()}).json()["cameras"][0]
        self.assertEqual({segment["source"] for segment in timeline["segments"] if segment["id"] in {item["id"] for item in self.backfilled()}}, {"backfill"})
        self.assertEqual({segment["source"] for segment in timeline["segments"]} - {"backfill"}, {"live"})
        reported = self.client.get(f"/api/cameras/{self.camera_id}/coverage").json()
        self.assertEqual([item["id"] for item in reported["backfilled"]], [item["id"] for item in self.backfilled()])
        report = coverage.report(START - timedelta(minutes=5), START + timedelta(minutes=5))
        self.assertAlmostEqual(next(camera for camera in report["cameras"] if camera["camera_id"] == self.camera_id)["backfilled_seconds"], OUTAGE, delta=1.5)

    def test_gaps_say_what_is_being_done_about_them(self):
        def gap_note() -> dict:
            return self.timeline()["gaps"][0]["backfill"]
        self.assertEqual(gap_note()["state"], "waiting")
        with connect() as db:
            db.execute("UPDATE cameras SET backfill_enabled=0")
        self.assertEqual(gap_note()["state"], "off")
        with connect() as db:
            db.execute("UPDATE cameras SET backfill_enabled=1,backfill_support='unsupported',backfill_support_message=?", (backfill.NOT_SUPPORTED,))
        self.assertEqual(gap_note(), {"state": "unsupported", "message": backfill.NOT_SUPPORTED})
        with connect() as db:
            db.execute("UPDATE cameras SET backfill_support=NULL")
            backfill.plan_jobs(db, self.camera_id, NOW)
        self.assertEqual(gap_note()["state"], "waiting")
        with connect() as db:
            db.execute("UPDATE backfill_jobs SET state='failed',message='Gave up'")
        self.assertEqual(gap_note(), {"state": "failed", "message": "Gave up"})

    def test_outages_the_schedule_expects_and_ones_still_going_on_are_left_alone(self):
        with connect() as db:
            db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,reason_code,starts_at,ends_at,checked_at) VALUES(?,?,?,?,?,?,?)",
                       (self.camera_id, "expected_idle", "No motion", "no_motion", START.isoformat(), RESUMED.isoformat(), RESUMED.isoformat()))
        self.assertEqual(self.run_backfill(), 0)
        self.assertEqual(self.jobs(), [])
        # The camera's latest outage reaches almost to now: it may not be over.
        recent = self.add_camera(self.camera)
        with connect() as db:
            db.execute("DELETE FROM recording_segments WHERE camera_id=? AND starts_at>?", (recent, (NOW - timedelta(hours=1)).isoformat()))
            db.execute("DELETE FROM recording_segments WHERE camera_id=?", (self.camera_id,))
            db.execute("UPDATE cameras SET backfill_enabled=0 WHERE id=?", (self.camera_id,))
            db.execute("DELETE FROM coverage_intervals")
        self.live_segment(recent, NOW - timedelta(minutes=10), 60)
        asyncio.run(backfill.BackfillManager().tick(NOW))
        self.assertEqual([job for job in self.jobs(recent) if job["ends_at"] > (NOW - timedelta(seconds=90)).isoformat()], [])

    def test_a_camera_that_is_not_switched_on_is_never_asked(self):
        with connect() as db:
            db.execute("UPDATE cameras SET backfill_enabled=0")
        self.run_backfill()
        self.assertEqual((self.jobs(), self.camera.requests), ([], []))


class ResumeTests(BackfillCase):
    def test_a_job_interrupted_after_a_chunk_resumes_where_it_stopped_after_a_restart(self):
        self.assertEqual(self.run_backfill(max_chunks=1), 1)
        stopped = self.jobs()[0]
        self.assertEqual(stopped["state"], "pending")
        self.assertEqual(stopped["cursor_at"], (START + timedelta(seconds=3)).isoformat())
        self.assertEqual(len(self.backfilled()), 1)
        # The application stopped while it was running: the new process starts it again from the cursor.
        with connect() as db:
            db.execute("UPDATE backfill_jobs SET state='running'")
        manager = backfill.BackfillManager()
        manager.recover()
        self.assertEqual(self.jobs()[0]["state"], "pending")
        self.camera.replay_requests.clear()
        asyncio.run(manager.tick(NOW))
        done = self.jobs()[0]
        self.assertEqual(done["state"], "done")
        segments = self.backfilled()
        self.assertEqual(segments[0]["starts_at"], START.isoformat())
        self.assertEqual(len({segment["starts_at"] for segment in segments}), len(segments))  # The first chunk was not fetched again
        self.assertAlmostEqual(done["filled_seconds"], OUTAGE, delta=1.5)

    def test_a_restart_with_nothing_left_to_do_changes_nothing(self):
        self.run_backfill()
        before = self.backfilled()
        backfill.BackfillManager().recover()
        self.assertEqual(self.run_backfill(), 0)
        self.assertEqual(self.backfilled(), before)
        self.assertEqual(len(self.jobs()), 1)

    def test_turning_it_off_part_way_stops_the_job_and_turning_it_on_continues(self):
        manager = backfill.BackfillManager()
        original = backfill.fetch_chunk

        async def fetch_then_switch_off(*args, **kwargs):
            await original(*args, **kwargs)
            with connect() as db:
                db.execute("UPDATE cameras SET backfill_enabled=0")
        with mock.patch.object(backfill, "fetch_chunk", fetch_then_switch_off):
            asyncio.run(manager.tick(NOW))
        self.assertEqual((self.jobs()[0]["state"], len(self.backfilled())), ("pending", 1))
        with connect() as db:
            db.execute("UPDATE cameras SET backfill_enabled=1")
        asyncio.run(manager.tick(NOW))
        self.assertEqual(self.jobs()[0]["state"], "done")


class BandwidthTests(BackfillCase):
    def test_the_read_rate_keeps_a_stream_at_or_under_the_limit(self):
        self.assertEqual(backfill.readrate_for(1000, None), 1.0)  # Until the bitrate is known: real time
        self.assertEqual(backfill.readrate_for(1000, 2000), 0.5)
        self.assertEqual(backfill.readrate_for(1000, 500), 2.0)
        self.assertEqual(backfill.readrate_for(1000, 1), 50.0)
        self.assertEqual(backfill.readrate_for(64, 100000), 0.02)

    def test_chunks_are_fetched_no_faster_than_the_configured_limit(self):
        seen: list[float] = []
        original = backfill.fetch_chunk

        async def watch(url, offset, duration, target, readrate):
            seen.append(readrate)
            await original(url, offset, duration, target, readrate)
        with connect() as db:
            db.execute("UPDATE cameras SET backfill_limit_kbps=?", (backfill.MIN_LIMIT_KBPS,))
        with mock.patch.object(backfill, "fetch_chunk", watch), mock.patch.object(backfill, "FIRST_CHUNK", timedelta(seconds=2)), mock.patch.object(backfill, "CHUNK", timedelta(seconds=2)):
            # Only the first two chunks: 2 s at the real-time rate to learn the bitrate, then 2 s of footage at the limit
            self.assertEqual(self.run_backfill(max_chunks=2), 2)
        first, second = self.backfilled()
        bitrate = first["size_bytes"] * 8 / 1000 / 2
        self.assertEqual(seen[0], 1.0)
        self.assertAlmostEqual(seen[1], backfill.MIN_LIMIT_KBPS / bitrate, delta=max(0.02, seen[1] * 0.1))
        job = self.jobs()[0]
        # What was fetched in the second chunk, over the time it took, is at or near the limit and well below the footage's own bitrate.
        self.assertLess(seen[1], 1.0)
        self.assertLessEqual(job["observed_kbps"], backfill.MIN_LIMIT_KBPS * 1.6)

    def test_the_limit_is_set_by_an_administrator_within_bounds(self):
        url = f"/api/cameras/{self.camera_id}/backfill"
        self.assertEqual(self.client.put(url, json={"enabled": True, "limit_kbps": 10}).status_code, 422)
        self.assertEqual(self.client.put(url, json={"enabled": True, "limit_kbps": 2_000_000}).status_code, 422)
        shown = self.client.put(url, json={"enabled": True, "limit_kbps": 2048}).json()
        self.assertEqual((shown["enabled"], shown["limit_kbps"], shown["default_limit_kbps"]), (True, 2048, backfill.DEFAULT_LIMIT_KBPS))
        self.assertEqual(self.client.put(url, json={"enabled": True}).json()["limit_kbps"], None)


class UnsupportedTests(BackfillCase):
    def setUp(self):
        super().setUp()
        self.plain = OnvifCamera(services=("media", "events"))
        self.plain_id = self.add_camera(self.plain)
        with connect() as db:
            db.execute("UPDATE cameras SET backfill_enabled=0 WHERE id=?", (self.camera_id,))

    def tearDown(self):
        self.plain.close()
        super().tearDown()

    def test_a_camera_without_profile_g_reports_it_is_not_supported_and_is_not_asked_again(self):
        self.run_backfill()
        job = self.jobs(self.plain_id)[0]
        self.assertEqual((job["state"], job["message"]), ("unsupported", backfill.NOT_SUPPORTED))
        support = backfill.support_view(self.plain_id)
        self.assertEqual((support["state"], support["message"]), ("unsupported", backfill.NOT_SUPPORTED))
        self.assertEqual(self.jobs(self.plain_id)[0]["attempts"], 0)
        asked = len(self.plain.requests)
        for _ in range(3):
            self.assertEqual(self.run_backfill(), 0)
        self.assertEqual(len(self.plain.requests), asked, "no retries")
        later = NOW + timedelta(hours=3)
        asyncio.run(backfill.BackfillManager().tick(later))
        self.assertEqual(len(self.plain.requests), asked)
        gap = self.client.post("/api/playback/timeline", json={"camera_ids": [self.plain_id], "start": (START - timedelta(minutes=2)).isoformat(),
                                                               "end": (START + timedelta(minutes=2)).isoformat()}).json()["cameras"][0]["gaps"][0]
        self.assertEqual(gap["backfill"], {"state": "unsupported", "message": backfill.NOT_SUPPORTED})

    def test_switching_it_off_and_on_again_asks_the_camera_afresh(self):
        self.run_backfill()
        asked = len(self.plain.requests)
        url = f"/api/cameras/{self.plain_id}/backfill"
        self.client.put(url, json={"enabled": False})
        self.client.put(url, json={"enabled": True})
        self.assertGreater(len(self.plain.requests), asked, "switching it on asks the camera at once, without waiting for an outage")
        self.assertEqual((backfill.support_view(self.plain_id)["state"], self.jobs(self.plain_id)[0]["state"]), ("unsupported", "unsupported"))

    def test_a_check_on_request_records_what_the_camera_offers(self):
        shown = self.client.post(f"/api/cameras/{self.plain_id}/backfill/check").json()
        self.assertEqual(shown["support"]["state"], "unsupported")
        shown = self.client.post(f"/api/cameras/{self.camera_id}/backfill/check").json()
        self.assertEqual((shown["support"]["state"], shown["support"]["message"]), ("supported", "The camera offers ONVIF recording search and replay"))

    def test_a_camera_with_no_onvif_address_cannot_be_backfilled(self):
        with connect() as db:
            db.execute("UPDATE cameras SET device_service_url=NULL WHERE id=?", (self.plain_id,))
        self.run_backfill()
        self.assertEqual(backfill.support_view(self.plain_id)["state"], "unsupported")
        self.assertEqual(self.jobs(self.plain_id)[0]["state"], "unsupported")


class FailureTests(BackfillCase):
    def test_a_camera_that_cannot_be_reached_is_tried_again_later_with_a_growing_wait_and_then_given_up_on(self):
        self.camera.close()
        self.run_backfill()
        job = self.jobs()[0]
        self.assertEqual((job["state"], job["attempts"]), ("pending", 1))
        self.assertEqual(datetime.fromisoformat(job["next_attempt_at"]) - NOW, backfill.backoff(1))
        self.assertEqual(self.run_backfill(), 0)  # Not due yet
        self.assertEqual(self.jobs()[0]["attempts"], 1)
        when = NOW
        for attempt in range(2, backfill.MAX_ATTEMPTS + 1):
            when += backfill.BACKOFF_MAX + timedelta(seconds=1)
            asyncio.run(backfill.BackfillManager().tick(when))
            self.assertEqual(self.jobs()[0]["attempts"], attempt)
        final = self.jobs()[0]
        self.assertEqual(final["state"], "failed")
        self.assertIn("Gave up", final["message"])
        self.assertEqual(backfill.support_view(self.camera_id)["state"], "unknown")  # Unreachable is not "unsupported"
        asyncio.run(backfill.BackfillManager().tick(when + timedelta(days=1)))
        self.assertEqual(self.jobs()[0]["attempts"], backfill.MAX_ATTEMPTS)
        self.assertEqual(backfill.backoff(1), timedelta(minutes=1))
        self.assertEqual(backfill.backoff(3), timedelta(minutes=4))
        self.assertEqual(backfill.backoff(20), backfill.BACKOFF_MAX)

    def test_a_transient_failure_part_way_keeps_the_chunks_already_fetched(self):
        original = backfill.fetch_chunk
        calls = []

        async def second_fails(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("The camera's replay could not be copied")
            await original(*args, **kwargs)
        with mock.patch.object(backfill, "fetch_chunk", second_fails):
            self.run_backfill()
        job = self.jobs()[0]
        self.assertEqual((job["state"], job["attempts"], len(self.backfilled())), ("pending", 1, 1))
        self.assertIn("Will try again", job["message"])
        asyncio.run(backfill.BackfillManager().tick(NOW + timedelta(minutes=5)))
        self.assertEqual(self.jobs()[0]["state"], "done")
        self.assertEqual(self.jobs()[0]["attempts"], 0)

    def test_a_camera_with_no_recording_of_that_time_is_not_retried(self):
        self.camera.recordings_present = False
        self.run_backfill()
        job = self.jobs()[0]
        self.assertEqual(job["state"], "failed")
        self.assertIn("no recording", job["message"])
        self.assertEqual(self.backfilled(), [])
        searches = self.camera.searches
        asyncio.run(backfill.BackfillManager().tick(NOW + timedelta(seconds=60)))
        self.assertEqual(self.camera.searches, searches)
        self.assertEqual(self.jobs()[0]["state"], "failed")

    def test_a_camera_that_recorded_only_part_of_the_outage_fills_that_part(self):
        self.camera.earliest = START + timedelta(seconds=6)
        self.camera.seconds = 60
        self.run_backfill()
        job = self.jobs()[0]
        self.assertEqual(job["state"], "partial")
        self.assertAlmostEqual(job["filled_seconds"], OUTAGE - 6, delta=1.5)
        self.assertEqual(self.backfilled()[0]["starts_at"], (START + timedelta(seconds=6)).isoformat())
        self.assertEqual(self.timeline()["gaps"][0]["backfill"]["state"], "partly filled")

    def test_a_job_never_leaves_the_credentials_in_what_it_says(self):
        self.camera.close()
        self.run_backfill()
        text = json.dumps(self.jobs()) + json.dumps(backfill.status(self.camera_id))
        self.assertNotIn(self.camera.password, text)
        self.assertNotIn(self.camera.username, text)


class RobustnessTests(BackfillCase):
    def test_live_footage_indexed_after_the_gap_was_found_is_not_fetched_over(self):
        with connect() as db:
            backfill.plan_jobs(db, self.camera_id, NOW)
        self.live_segment(self.camera_id, START + timedelta(seconds=4), 4)  # Late-indexed live footage inside the gap
        self.run_backfill()
        live_from, live_to = (START + timedelta(seconds=4)).isoformat(), (START + timedelta(seconds=8)).isoformat()
        for segment in self.backfilled():
            self.assertTrue(segment["ends_at"] <= live_from or segment["starts_at"] >= live_to, segment)
        self.assertEqual(self.jobs()[0]["state"], "done")
        self.assertAlmostEqual(self.jobs()[0]["filled_seconds"], OUTAGE - 4, delta=1.5)

    def test_an_unexpected_error_leaves_the_job_waiting_to_try_again_not_stuck_running(self):
        async def broken(*args, **kwargs):
            raise ValueError("something unexpected")
        with mock.patch.object(backfill, "fetch_chunk", broken):
            self.run_backfill()
        job = self.jobs()[0]
        self.assertEqual((job["state"], job["attempts"]), ("pending", 1))
        self.assertIn("something unexpected", job["message"])

    def test_the_camera_login_is_never_in_the_arguments_of_the_process_that_fetches(self):
        seen: list[tuple] = []
        original = asyncio.create_subprocess_exec

        async def watch(*args, **kwargs):
            seen.append(args)
            return await original(*args, **kwargs)
        with mock.patch.object(asyncio, "create_subprocess_exec", watch):
            self.run_backfill(max_chunks=1)
        fetches = [args for args in seen if "-readrate" in args]
        self.assertTrue(fetches)
        text = " ".join(str(item) for args in fetches for item in args)
        self.assertNotIn(self.camera.password, text)
        self.assertNotIn(self.camera.username, text)
        self.assertNotIn("replay", text)

    def test_a_chunk_fetched_again_after_an_interruption_replaces_its_row_instead_of_leaving_stale_figures(self):
        path = Path(self.temporary.name) / "recordings" / self.camera_id / "backfill-x.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 300)
        backfill.BackfillManager._store(self.camera_id, path, START, START + timedelta(seconds=3), 300)
        path.write_bytes(b"x" * 500)
        backfill.BackfillManager._store(self.camera_id, path, START, START + timedelta(seconds=4), 500)
        rows = self.backfilled()
        self.assertEqual([(row["size_bytes"], row["ends_at"]) for row in rows], [(500, (START + timedelta(seconds=4)).isoformat())])

    def test_a_chunk_that_comes_out_short_leaves_no_hole(self):
        original = backfill._probe_seconds
        calls = []

        async def short_first(path):
            calls.append(1)
            return min(await original(path), 1.0) if len(calls) == 1 else await original(path)
        with mock.patch.object(backfill, "_probe_seconds", short_first):
            self.run_backfill()
        segments = self.backfilled()
        self.assertEqual(self.jobs()[0]["state"], "done")
        self.assertEqual(segments[0]["ends_at"], (START + timedelta(seconds=1)).isoformat())
        self.assertEqual(segments[1]["starts_at"], segments[0]["ends_at"])  # The next chunk starts where the short one ended

    def test_a_gap_whose_backfilled_footage_retention_removed_says_so_and_a_failed_job_is_retried_when_switched_on_again(self):
        self.run_backfill()
        with connect() as db:
            db.execute("DELETE FROM recording_segments WHERE source='backfill'")
        gap = self.timeline()["gaps"][0]["backfill"]
        self.assertEqual(gap["state"], "removed")
        with connect() as db:
            db.execute("UPDATE backfill_jobs SET state='failed',attempts=6,message='Gave up'")
        url = f"/api/cameras/{self.camera_id}/backfill"
        self.client.put(url, json={"enabled": False})
        self.client.put(url, json={"enabled": True})
        job = self.jobs()[0]
        self.assertEqual((job["state"], job["attempts"]), ("pending", 0))
        self.run_backfill()
        self.assertEqual(self.jobs()[0]["state"], "done")


class ApiTests(BackfillCase):
    def test_status_shows_the_setting_support_and_recent_jobs(self):
        self.run_backfill()
        shown = self.client.get(f"/api/cameras/{self.camera_id}/backfill").json()
        self.assertEqual((shown["enabled"], shown["support"]["state"]), (True, "supported"))
        self.assertEqual([job["state"] for job in shown["jobs"]], ["done"])
        self.assertGreater(shown["jobs"][0]["bytes_fetched"], 0)
        self.assertEqual(self.client.get(f"/api/cameras/{uuid.uuid4()}/backfill").status_code, 404)

    def test_only_administrators_change_it_and_changes_are_audited(self):
        self.user = ("operator", "operator")
        url = f"/api/cameras/{self.camera_id}/backfill"
        self.assertEqual(self.client.put(url, json={"enabled": False}).status_code, 403)
        self.assertEqual(self.client.post(f"{url}/check").status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.user = ("admin", "administrator")
        self.client.put(url, json={"enabled": False, "limit_kbps": 512})
        with connect() as db:
            event = db.execute("SELECT actor_id,details_json FROM audit_log WHERE action='backfill_settings_changed'").fetchone()
        self.assertEqual(event["actor_id"], 1)
        self.assertEqual(json.loads(event["details_json"]), {"enabled": False, "limit_kbps": 512, "was_enabled": True, "was_limit_kbps": None})


if __name__ == "__main__":
    unittest.main()
