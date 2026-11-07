"""Live video kept ready (#216): the first viewer of a camera joins a stream that is already running."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import live_ready, live_variant, privacy
from app.credentials import encrypt
from app.live_hub import LiveHub, LiveStream, Viewer
from app.main import app
from app.principal import current_user
from app.store import connect, initialize
from tests.test_support import principal


class Case(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.directory.name) / "state.sqlite3")
        initialize()
        self.start = patch.object(LiveStream, "start", lambda stream: None)  # A held stream is not given a real FFmpeg here
        self.start.start()

    def tearDown(self):
        self.start.stop()
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.directory.cleanup()

    def camera(self, camera_id: str, name: str, *, sub: bool = False, codec: str | None = "h264", archived: bool = False, enabled: bool = True, audio: bool = False) -> None:
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,live_substream_url_ciphertext,video_codec,audio_enabled,enabled,archived_at) VALUES(?,?,?,?,?,?,?,?)",
                       (camera_id, name, encrypt(f"rtsp://192.0.2.1/{camera_id}/main"), encrypt(f"rtsp://192.0.2.1/{camera_id}/sub") if sub else None, codec, int(audio), int(enabled),
                        "2026-01-01T00:00:00Z" if archived else None))

    def wanted(self):
        with connect() as db:
            return live_ready.wanted(db)


class WantedTests(Case):
    def test_each_camera_is_held_as_a_grid_viewer_would_be_given_it(self):
        self.camera("a", "Alpha")  # No substream: its main stream is the grid stream
        self.camera("b", "Bravo", sub=True)  # A substream: that is what a grid plays
        keys = {key[0]: key for key, _ in self.wanted()}
        self.assertEqual((keys["a"][1], keys["b"][1]), ("main", "sub"))
        self.assertEqual({how["source"] for _, how in self.wanted()}, {"rtsp://192.0.2.1/a/main", "rtsp://192.0.2.1/b/sub"})

    def test_the_held_stream_has_the_key_a_viewer_gets(self):
        self.camera("a", "Alpha", audio=True)
        with connect() as db:
            row = db.execute("SELECT stream_url_ciphertext,live_substream_url_ciphertext,video_codec,audio_enabled FROM cameras WHERE id='a'").fetchone()
            viewer_variant = live_variant.choose(row, privacy.server_zones(db, "a"), "low", False)
        from app.credentials import decrypt
        viewer_key = live_variant.key_of("a", viewer_variant, decrypt(row["stream_url_ciphertext"]))
        self.assertEqual([key for key, _ in self.wanted()], [viewer_key])

    def test_archived_disabled_and_converted_cameras_are_left_out_and_the_rest_are_capped(self):
        self.camera("a", "Alpha")
        self.camera("b", "Bravo", archived=True)
        self.camera("c", "Charlie", enabled=False)
        self.camera("d", "Delta", codec="hevc")  # Would be converted to H.264 for a browser that cannot play H.265
        self.camera("e", "Echo", codec="mjpeg")  # No stream a browser can play directly
        self.assertEqual([key[0] for key, _ in self.wanted()], ["a"])
        for number in range(40):
            self.camera(f"x{number:02}", f"Extra {number:02}")
        self.assertEqual(len(self.wanted()), live_ready.MAX_READY_STREAMS)

    def test_a_camera_with_an_unusable_address_is_skipped_not_fatal(self):
        self.camera("a", "Alpha")
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('z','Zulu',?)", (encrypt("file:///etc/passwd"),))
        self.assertEqual([key[0] for key, _ in self.wanted()], ["a"])


class KeeperTests(Case):
    def keeper(self):
        hub = LiveHub()
        return hub, live_ready.LiveReadyKeeper(hub)

    def test_all_holds_each_stream_once_and_a_viewer_joins_the_held_stream(self):
        async def go():
            self.camera("a", "Alpha")
            self.camera("b", "Bravo")
            hub, keeper = self.keeper()
            desired = self.wanted()
            keeper.apply("all", desired, now=0.0)
            keeper.apply("all", desired, now=1.0)
            self.assertEqual(len(hub.streams), 2)
            self.assertTrue(all(len(stream.viewers) == 1 for stream in hub.streams.values()))  # Just the one that keeps it open
            key, how = desired[0]
            held = hub.streams[key]
            held.ready.set()
            viewer = Viewer()
            joined = hub.join(key, how["source"], how["include_audio"], viewer, None, how["video_filter"], how["transcode"], how["hevc_copy"])
            self.assertIs(joined, held)  # The viewer joined the running stream: no second connection
            self.assertEqual(len(hub.streams), 2)
        asyncio.run(go())

    def test_the_hub_snapshot_counts_people_watching_not_the_keeper(self):
        async def go():
            self.camera("a", "Alpha")
            hub, keeper = self.keeper()
            key, how = self.wanted()[0]
            keeper.apply("all", [(key, how)], now=0.0)
            self.assertEqual([(item["viewers"], item["held_ready"]) for item in hub.snapshot()], [(0, True)])
            hub.join(key, how["source"], how["include_audio"], Viewer(), None, None, False, False)
            self.assertEqual([(item["viewers"], item["held_ready"]) for item in hub.snapshot()], [(1, True)])
        asyncio.run(go())

    def test_a_viewer_joining_a_held_stream_sees_the_cameras_live_status_kept_fresh(self):
        from app.api import live_media

        async def go():
            self.camera("a", "Alpha")
            hub, keeper = self.keeper()
            key, how = self.wanted()[0]
            keeper.apply("all", [(key, how)], now=0.0)
            self.assertTrue(live_media._acquire_live_view("a"))  # What a viewer's connection does first
            try:
                self.assertIsNone(live_media._live_camera_status["a"]["last_frame_at"])
                hub.streams[key].on_fragment()  # A frame goes out on the stream the keeper started
                self.assertIsNotNone(live_media._live_camera_status["a"]["last_frame_at"])
            finally:
                live_media._release_live_view("a")
        asyncio.run(go())

    def test_a_held_stream_is_not_closed_when_the_last_viewer_leaves(self):
        async def go():
            self.camera("a", "Alpha")
            hub, keeper = self.keeper()
            key, how = self.wanted()[0]
            keeper.apply("all", [(key, how)], now=0.0)
            viewer = Viewer()
            stream = hub.join(key, how["source"], how["include_audio"], viewer, None, None, False, False)
            hub.leave(stream, viewer)
            with patch("app.live_hub.LINGER_SECONDS", 0.05):
                await asyncio.sleep(0.2)
            self.assertIn(key, hub.streams)
        asyncio.run(go())

    def test_recent_holds_nothing_ahead_but_lets_a_watched_stream_linger_ten_minutes(self):
        async def go():
            self.camera("a", "Alpha")
            hub, keeper = self.keeper()
            keeper.apply("recent", self.wanted(), now=0.0)
            self.assertEqual((len(hub.streams), hub.linger_seconds), (0, live_ready.RECENT_LINGER_SECONDS))
            key, how = self.wanted()[0]
            stream = hub.join(key, how["source"], how["include_audio"], Viewer(), None, None, False, False)
            self.assertEqual(stream.linger_seconds, live_ready.RECENT_LINGER_SECONDS)
        asyncio.run(go())

    def test_off_holds_nothing_and_goes_back_to_the_short_default(self):
        async def go():
            self.camera("a", "Alpha")
            hub, keeper = self.keeper()
            keeper.apply("all", self.wanted(), now=0.0)
            stream = next(iter(hub.streams.values()))
            keeper.apply("off", self.wanted(), now=1.0)
            self.assertEqual((keeper.held, hub.linger_seconds, stream.linger_seconds, stream.viewers), ({}, None, None, set()))
        asyncio.run(go())

    def test_a_failed_stream_is_tried_again_after_a_gap_that_grows(self):
        async def go():
            self.camera("a", "Alpha")
            hub, keeper = self.keeper()
            desired = self.wanted()
            key = desired[0][0]
            keeper.apply("all", desired, now=0.0)
            first = hub.streams[key]
            first.failed = "The camera did not start"
            keeper.apply("all", desired, now=1.0)  # Let go of it, and wait
            self.assertNotIn(key, keeper.held)
            keeper.apply("all", desired, now=5.0)  # Still inside the first gap (15 s)
            self.assertNotIn(key, keeper.held)
            keeper.apply("all", desired, now=17.0)  # After it: held again, as a new stream
            second = hub.streams[key]
            self.assertIsNot(second, first)
            self.assertIn(key, keeper.held)
            second.failed = "again"
            keeper.apply("all", desired, now=18.0)
            keeper.apply("all", desired, now=40.0)  # The second gap is 30 s from 18
            self.assertNotIn(key, keeper.held)
            keeper.apply("all", desired, now=49.0)
            self.assertIn(key, keeper.held)
        asyncio.run(go())

    def test_a_camera_that_is_removed_stops_being_held(self):
        async def go():
            self.camera("a", "Alpha")
            self.camera("b", "Bravo")
            hub, keeper = self.keeper()
            desired = self.wanted()
            keeper.apply("all", desired, now=0.0)
            keeper.apply("all", desired[:1], now=1.0)
            self.assertEqual([key[0] for key in keeper.held], ["a"])
        asyncio.run(go())


class SettingsApiTests(Case):
    def setUp(self):
        super().setUp()
        app.dependency_overrides[current_user] = lambda: principal(1, "admin", "administrator")
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        app.dependency_overrides.pop(current_user, None)
        super().tearDown()

    def test_the_mode_defaults_to_all_and_an_administrator_can_change_it_with_an_audit_entry(self):
        body = self.client.get("/api/live-ready").json()
        self.assertEqual((body["mode"], [choice["id"] for choice in body["choices"]], body["limit"]), ("all", ["all", "recent", "off"], live_ready.MAX_READY_STREAMS))
        response = self.client.put("/api/live-ready", json={"mode": "recent"})
        self.assertEqual((response.status_code, response.json()["mode"]), (200, "recent"))
        self.assertEqual(self.client.get("/api/live-ready").json()["mode"], "recent")
        with connect() as db:
            self.assertEqual(db.execute("SELECT details_json FROM audit_log WHERE action='live_ready_updated'").fetchone()[0], json.dumps({"from": "all", "to": "recent"}, separators=(",", ":")))
        self.assertEqual(self.client.put("/api/live-ready", json={"mode": "sometimes"}).status_code, 422)

    def test_an_operator_cannot_read_or_change_it(self):
        app.dependency_overrides[current_user] = lambda: principal(2, "operator", "operator")
        self.assertEqual(self.client.get("/api/live-ready").status_code, 403)
        self.assertEqual(self.client.put("/api/live-ready", json={"mode": "off"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
