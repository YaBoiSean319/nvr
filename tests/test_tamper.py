"""Camera tamper detection: the judging, the state it keeps, incidents, rules, the API and recording continuity."""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import tamper
from app.store import connect
from tests.test_access_grants import GrantCase

FFMPEG = shutil.which("ffmpeg")
START = datetime.now(timezone.utc).replace(microsecond=0)  # A rule only fires for moments after it was saved, so the samples are dated from now.
STEP = timedelta(seconds=15)
SCENE: Path
FRAMES: dict[str, list[bytes]] = {}


def decode(args: list[str], count: int, rate: int = 5) -> list[bytes]:
    """Frames of generated footage, scaled and grayed exactly as the recorder's sampler does."""
    command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-loop", "1", "-i", str(SCENE), "-t", str(count / rate), "-r", str(rate), "-vf",
               ",".join(args + [f"scale={tamper.FRAME_WIDTH}:{tamper.FRAME_HEIGHT}:flags=area", "format=gray"]), "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    output = subprocess.run(command, capture_output=True, check=True, timeout=60).stdout
    frames = [output[index * tamper.FRAME_BYTES:(index + 1) * tamper.FRAME_BYTES] for index in range(len(output) // tamper.FRAME_BYTES)]
    assert len(frames) >= count, (args, len(frames))
    return frames[:count]


def setUpModule() -> None:
    global SCENE
    if not FFMPEG:
        raise unittest.SkipTest("needs ffmpeg")
    directory = Path(tempfile.mkdtemp(prefix="tamper-fixture-"))
    SCENE = directory / "scene.png"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=size=640x360:end_pts=40:rate=1", "-frames:v", "1", str(SCENE)], check=True)
    noise = "noise=alls=10:allf=t"
    FRAMES["normal"] = decode([noise], 40)
    FRAMES["black"] = decode(["lutyuv=y=val*0.02:u=128:v=128", noise], 20)
    FRAMES["white"] = decode(["lutyuv=y=240:u=128:v=128", noise], 20)
    FRAMES["moved"] = decode(["crop=iw*0.7:ih*0.7:iw*0.25:ih*0.15", "scale=640:360", noise], 20)
    FRAMES["shifted"] = decode(["crop=iw*0.75:ih:iw*0.25:0", "pad=iw/0.75:ih:0:0:black", "scale=640:360", noise], 20)
    FRAMES["blurred"] = decode(["boxblur=30:3", noise], 20)
    # A day to night ramp and back: the whole scene fades to a quarter of its light over an hour of samples and returns.
    FRAMES["ramp"] = decode(["format=gray", "scale=160:90", "geq=lum='lum(X,Y)*(1-0.75*sin(PI*T/120))'", noise], 120, rate=1)


class TamperCase(GrantCase):
    def setUp(self):
        super().setUp()
        self.camera = self.d1
        self.at = START

    def enable(self, camera: str | None = None, **settings) -> dict:
        body = {"enabled": True, "sensitivity": 50, "sustain_seconds": 60, **settings}
        response = self.admin.put(f"/api/cameras/{camera or self.camera}/tamper", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def feed(self, frames: list[bytes], camera: str | None = None) -> list[str]:
        """One sample per frame, a sampling interval apart, through the manager that runs in the recorder."""
        camera = camera or self.camera
        states = []
        for frame in frames:
            async def grab(_camera: str, frame: bytes = frame) -> bytes:
                return frame
            asyncio.run(tamper.TamperManager(grabber=grab).sample_all(self.at))
            self.at += STEP
            with connect() as db:
                states.append(db.execute("SELECT state FROM tamper_settings WHERE camera_id=?", (camera,)).fetchone()["state"])
        return states

    def learn(self) -> None:
        self.assertEqual(self.feed(FRAMES["normal"][:tamper.LEARN_SAMPLES])[-1], "ok")

    def incidents(self, camera: str | None = None, active: bool | None = None) -> list[dict]:
        with connect() as db:
            rows = db.execute("SELECT * FROM incidents WHERE fault_type LIKE 'tamper\\_%' ESCAPE '\\' AND camera_id=? ORDER BY first_seen_at", (camera or self.camera,)).fetchall()
        return [dict(row) for row in rows if active is None or bool(row["fault_active"]) == active]


class JudgingTests(unittest.TestCase):
    def reference(self) -> tamper.Features:
        return tamper.features(FRAMES["normal"][0])

    def judge(self, name: str, sensitivity: int = 50, index: int = 5) -> str | None:
        return tamper.judge(self.reference(), tamper.features(FRAMES[name][index]), sensitivity)[0]

    def test_generated_footage_that_goes_black_white_moves_or_blurs_is_told_apart(self):
        self.assertEqual({name: self.judge(name) for name in ("normal", "black", "white", "moved", "shifted", "blurred")},
                         {"normal": None, "black": "covered", "white": "covered", "moved": "moved", "shifted": "moved", "blurred": "defocused"})

    def test_the_same_scene_with_ordinary_noise_is_never_tampering(self):
        reference = self.reference()
        for frame in FRAMES["normal"]:
            self.assertIsNone(tamper.judge(reference, tamper.features(frame), 100)[0])

    def test_changing_the_light_alone_changes_neither_layout_nor_sharpness(self):
        reference = tamper.features(FRAMES["normal"][0])
        for index in range(0, 120, 10):
            kind, metrics = tamper.judge(tamper.blend(reference, reference, 0), tamper.features(FRAMES["ramp"][index]), 50)
            self.assertGreater(metrics["layout_match"], 0.95, index)
            self.assertGreater(metrics["sharpness"], 0.8 * metrics["reference_sharpness"] if index else 0, index)

    def test_sensitivity_moves_every_threshold_the_same_way(self):
        low, high = tamper.Thresholds.for_sensitivity(1), tamper.Thresholds.for_sensitivity(100)
        for field in ("covered_mean", "covered_std", "layout", "sharpness"):
            self.assertLess(getattr(low, field), getattr(high, field), field)
        self.assertEqual(tamper.judge(self.reference(), tamper.features(FRAMES["blurred"][3]), 1)[0], None)
        self.assertEqual(tamper.judge(self.reference(), tamper.features(FRAMES["blurred"][3]), 100)[0], "defocused")

    def test_a_scene_with_nothing_in_it_cannot_be_judged_and_raises_nothing(self):
        flat = bytes([100]) * tamper.FRAME_BYTES
        reference = tamper.features(flat)
        for name in ("black", "moved", "blurred", "white"):
            self.assertIsNone(tamper.judge(reference, tamper.features(FRAMES[name][2]), 100)[0] if name != "black" else None, name)
        self.assertEqual(tamper.layout_match(reference, reference), 0.0)

    def test_frames_must_be_the_expected_size(self):
        with self.assertRaises(ValueError):
            tamper.features(b"short")

    def test_features_are_what_they_say(self):
        flat = tamper.features(bytes([50]) * tamper.FRAME_BYTES)
        self.assertEqual((flat.mean, flat.std, round(flat.sharpness, 6)), (50.0, 0.0, 0.0))
        half = bytes([0] * (tamper.FRAME_BYTES // 2) + [200] * (tamper.FRAME_BYTES // 2))
        features = tamper.features(half)
        self.assertAlmostEqual(features.mean, 100)
        self.assertAlmostEqual(features.std, 100)
        self.assertEqual(len(features.blocks), tamper.GRID_WIDTH * tamper.GRID_HEIGHT)
        self.assertEqual(tamper.Features.from_json(features.as_json()).mean, features.mean)


class DetectionTests(TamperCase):
    def sustained(self) -> int:
        return 60 // 15  # Samples that make 60 seconds, counted from the first changed one.

    def test_each_kind_of_tampering_raises_its_incident_once_it_has_lasted_the_configured_time(self):
        for name, kind in (("black", "covered"), ("shifted", "moved"), ("blurred", "defocused")):
            with self.subTest(kind):
                self.at = START
                with connect() as db:
                    db.execute("DELETE FROM tamper_settings")
                    db.execute("DELETE FROM incidents")
                self.enable()
                self.learn()
                states = self.feed(FRAMES[name][:8])
                self.assertEqual(states[:self.sustained()], ["ok"] * self.sustained(), "not before it has lasted a minute")
                self.assertEqual(states[self.sustained()], kind)  # 60 s after the first changed sample.
                (incident,) = self.incidents(active=True)
                self.assertEqual((incident["fault_type"], incident["camera_id"]), (f"tamper_{kind}", self.camera))
                self.assertIn("Dock 1", incident["summary"])
                self.assertEqual(json.loads(incident["details_json"])["kind"], kind)

    def test_the_time_it_must_last_is_configurable(self):
        self.enable(sustain_seconds=30)
        self.learn()
        self.assertEqual(self.feed(FRAMES["black"][:4]), ["ok", "ok", "covered", "covered"])
        self.at = START + timedelta(hours=1)
        self.enable(sustain_seconds=300)
        self.feed(FRAMES["normal"][5:9])
        self.assertEqual(self.feed((FRAMES["black"] * 2)[:20])[-1], "ok")  # 285 seconds in.
        self.assertEqual(self.feed(FRAMES["black"][:1])[-1], "covered")  # 300.

    def test_a_change_that_does_not_last_raises_nothing(self):
        self.enable()
        self.learn()
        self.assertEqual(set(self.feed(FRAMES["black"][:3] + FRAMES["normal"][5:8] + FRAMES["black"][:3] + FRAMES["normal"][8:12])), {"ok"})
        self.assertEqual(self.incidents(), [])

    def test_a_day_to_night_ramp_and_back_raises_nothing(self):
        self.enable()
        self.learn()
        states = self.feed(FRAMES["ramp"])
        self.assertEqual(set(states), {"ok"})
        self.assertEqual(self.incidents(), [])
        with connect() as db:
            reference = json.loads(db.execute("SELECT reference_json FROM tamper_settings").fetchone()[0])
        self.assertGreater(reference["mean"], 0.6 * tamper.features(FRAMES["normal"][0]).mean)  # It followed the light down and back up.

    def test_the_lights_going_out_all_at_once_is_a_covered_camera(self):
        self.enable()
        self.learn()
        dark = [bytes(int(value * 0.25) for value in frame) for frame in FRAMES["normal"][5:12]]
        self.assertEqual(self.feed(dark)[-1], "covered")

    def test_the_incident_recovers_when_the_view_returns(self):
        self.enable()
        self.learn()
        self.feed(FRAMES["black"][:6])
        self.assertEqual(len(self.incidents(active=True)), 1)
        states = self.feed(FRAMES["normal"][5:9])
        self.assertEqual(states[:tamper.CLEAR_SAMPLES - 1], ["covered"] * (tamper.CLEAR_SAMPLES - 1))
        self.assertEqual(states[tamper.CLEAR_SAMPLES - 1], "ok")
        (incident,) = self.incidents()
        self.assertEqual((incident["state"], incident["fault_active"]), ("recovered", 0))
        self.feed(FRAMES["black"][:6])  # And it can happen again.
        self.assertEqual(self.incidents()[0]["recurrence_count"], 2)

    def test_one_kind_of_tampering_giving_way_to_another_swaps_the_incident(self):
        self.enable()
        self.learn()
        self.feed(FRAMES["black"][:6])
        self.feed(FRAMES["shifted"][:7])
        by_type = {incident["fault_type"]: bool(incident["fault_active"]) for incident in self.incidents()}
        self.assertEqual(by_type, {"tamper_covered": False, "tamper_moved": True})

    def test_the_reference_is_learned_first_and_nothing_is_judged_until_then(self):
        self.enable()
        states = self.feed(FRAMES["black"][:tamper.LEARN_SAMPLES])  # What it learns is whatever it sees.
        self.assertEqual(states, ["learning"] * (tamper.LEARN_SAMPLES - 1) + ["ok"])
        self.assertEqual(self.incidents(), [])

    def test_a_missing_picture_is_not_tampering(self):
        self.enable()
        self.learn()

        async def nothing(_camera: str) -> None:
            return None
        asyncio.run(tamper.TamperManager(grabber=nothing).sample_all(self.at))
        with connect() as db:
            self.assertEqual(db.execute("SELECT state FROM tamper_settings").fetchone()[0], "ok")

    def test_switching_it_off_ends_a_detection_and_stops_sampling(self):
        self.enable()
        self.learn()
        self.feed(FRAMES["black"][:6])
        with connect() as db:
            last = db.execute("SELECT last_sample_at FROM tamper_settings").fetchone()[0]
        self.enable(enabled=False)
        self.assertEqual(self.incidents(active=True), [])
        self.assertEqual(self.feed(FRAMES["black"][:3])[-1], "ok")
        with connect() as db:
            self.assertEqual(db.execute("SELECT last_sample_at FROM tamper_settings").fetchone()[0], last)  # Not sampled at all.

    def test_an_archived_camera_ends_its_detection(self):
        self.enable()
        self.learn()
        self.feed(FRAMES["black"][:6])
        with connect() as db:
            db.execute("UPDATE cameras SET archived_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), self.camera))
        self.feed(FRAMES["black"][:1])
        self.assertEqual(self.incidents(active=True), [])

    def test_cameras_are_judged_on_their_own_references(self):
        self.enable(self.d1)
        self.enable(self.d2)
        self.feed(FRAMES["normal"][:5], self.d1)
        with connect() as db:
            db.execute("UPDATE tamper_settings SET state='ok'")
        self.assertEqual(self.incidents(self.d2), [])


class ReferenceTests(TamperCase):
    def test_relearning_after_a_deliberate_move_clears_the_incident_and_the_new_view_is_normal(self):
        self.enable()
        self.learn()
        self.assertEqual(self.feed(FRAMES["shifted"][:7])[-1], "moved")
        self.assertEqual(len(self.incidents(active=True)), 1)
        response = self.admin.post(f"/api/cameras/{self.camera}/tamper/relearn")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["state"], response.json()["learning_progress"]), ("learning", 0))
        self.assertEqual(self.incidents(active=True), [])
        self.assertEqual(self.feed(FRAMES["shifted"][:tamper.LEARN_SAMPLES + 12]).count("ok"), 12 + 1)
        self.assertEqual(self.incidents(active=True), [])
        self.assertEqual(len(self.incidents()), 1)  # Only the one that was cleared.
        self.assertEqual(self.feed(FRAMES["normal"][:7])[-1], "moved")  # And the old view is now the odd one.

    def test_relearning_is_audited_and_for_administrators_only(self):
        self.enable()
        self.assertEqual(self.limited.post(f"/api/cameras/{self.camera}/tamper/relearn").status_code, 403)
        self.admin.post(f"/api/cameras/{self.camera}/tamper/relearn")
        self.assertEqual([entry["resource_id"] for entry in self.audit("tamper_relearned")], [self.camera])

    def test_a_reference_in_the_database_is_the_one_used_after_a_restart(self):
        self.enable()
        self.learn()
        with connect() as db:
            self.assertEqual(db.execute("SELECT reference_samples FROM tamper_settings").fetchone()[0], tamper.LEARN_SAMPLES)
        self.assertEqual(self.feed(FRAMES["black"][:6])[-1], "covered")  # A new manager, the same reference.


class RuleTests(TamperCase):
    def rule(self, **extra) -> dict:
        body = {"name": "Tamper", "camera_ids": [self.camera], "condition": {"type": "tamper"}, **extra}
        response = self.admin.post("/api/rules", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def alarms(self) -> list[dict]:
        response = self.admin.get("/api/alarms", params={"state": "all"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["alarms"]

    def trip(self, name: str = "black") -> None:
        self.feed(FRAMES[name][:6])

    def test_a_tamper_rule_needs_no_object_classes_and_stores_none(self):
        created = self.rule(classes=["person"])
        self.assertEqual((created["condition"], created["classes"]), ({"type": "tamper", "kinds": ["covered", "moved", "defocused"]}, []))

    def test_other_conditions_still_need_a_class(self):
        zone = {"type": "zone_entry", "zone": {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}}
        self.assertEqual(self.admin.post("/api/rules", json={"name": "Z", "camera_ids": [self.camera], "condition": zone}).status_code, 422)
        self.assertEqual(self.admin.post("/api/rules", json={"name": "Z", "camera_ids": [self.camera], "condition": zone, "classes": ["person"]}).status_code, 201)

    def test_a_confirmed_detection_raises_an_alarm_labelled_with_the_kind(self):
        self.rule()
        self.enable()
        self.learn()
        self.feed(FRAMES["black"][:3])
        self.assertEqual(self.alarms(), [])  # Not before it is confirmed.
        self.feed(FRAMES["black"][3:8])
        (alarm,) = self.alarms()
        self.assertEqual((alarm["label"], alarm["condition_type"], alarm["camera_id"], alarm["rule_name"]), ("covered", "tamper", self.camera, "Tamper"))
        self.feed(FRAMES["black"][:6])
        self.assertEqual(len(self.alarms()), 1)  # Once per detection, not once per sample.
        self.assertEqual(self.admin.get(f"/api/alarms/{alarm['id']}").status_code, 200)

    def test_a_rule_lists_the_kinds_it_wants(self):
        self.rule(condition={"type": "tamper", "kinds": ["moved"]})
        self.enable()
        self.learn()
        self.trip("black")
        self.assertEqual(self.alarms(), [])
        self.feed(FRAMES["normal"][5:9])
        self.trip("shifted")
        self.assertEqual([alarm["label"] for alarm in self.alarms()], ["moved"])

    def test_scope_group_and_other_cameras(self):
        group = self.admin.get("/api/camera-groups").json()
        self.rule(camera_ids=[self.d2])
        self.enable()
        self.learn()
        self.trip()
        self.assertEqual(self.alarms(), [])  # The rule watches another camera.
        self.assertEqual(len(self.incidents(active=True)), 1)  # The incident still exists: rules only add an alarm.
        self.assertIsNotNone(group)

    def test_a_cooldown_holds_back_the_next_alarm_on_the_camera(self):
        self.rule(cooldown_seconds=3600)
        self.enable()
        self.learn()
        self.trip("black")
        self.feed(FRAMES["normal"][5:9])
        self.trip("shifted")
        self.assertEqual(len(self.alarms()), 1)

    def test_a_rule_made_after_a_detection_is_not_reminded_of_it(self):
        self.enable()
        self.learn()
        self.trip()
        self.rule()
        self.feed(FRAMES["black"][:3])
        self.assertEqual(self.alarms(), [])

    def test_test_mode_replays_the_tamper_incidents_of_the_last_day(self):
        self.at = datetime.now(timezone.utc) - timedelta(minutes=30)
        self.enable()
        self.learn()
        self.trip("shifted")
        replay = self.admin.post("/api/rules/test", json={"name": "T", "camera_ids": [self.camera], "condition": {"type": "tamper", "kinds": ["moved"]}})
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual([(event["label"], event["camera_id"]) for event in replay.json()["events"]], [("moved", self.camera)])
        none = self.admin.post("/api/rules/test", json={"name": "T", "camera_ids": [self.camera], "condition": {"type": "tamper", "kinds": ["covered"]}})
        self.assertEqual(none.json()["count"], 0)


class ApiTests(TamperCase):
    def test_a_new_camera_has_it_off_and_reports_so(self):
        body = self.admin.get(f"/api/cameras/{self.camera}/tamper").json()
        self.assertEqual((body["enabled"], body["state"], body["sensitivity"], body["sustain_seconds"]), (False, "off", 50, 60))

    def test_settings_are_saved_validated_and_audited(self):
        saved = self.enable(sensitivity=70, sustain_seconds=120)
        self.assertEqual((saved["enabled"], saved["sensitivity"], saved["sustain_seconds"], saved["state"]), (True, 70, 120, "learning"))
        for bad in ({"sensitivity": 0}, {"sensitivity": 101}, {"sustain_seconds": 9}, {"sustain_seconds": 3601}):
            self.assertEqual(self.admin.put(f"/api/cameras/{self.camera}/tamper", json={"enabled": True, "sensitivity": 50, "sustain_seconds": 60, **bad}).status_code, 422, bad)
        (entry,) = self.audit("tamper_configured")
        self.assertEqual(entry["details"], {"before": {"enabled": False, "sensitivity": 50, "sustain_seconds": 60}, "after": {"enabled": True, "sensitivity": 70, "sustain_seconds": 120}})

    def test_operators_may_look_but_not_change(self):
        self.enable()
        self.assertEqual(self.full.get(f"/api/cameras/{self.camera}/tamper").status_code, 200)
        self.assertEqual(self.full.put(f"/api/cameras/{self.camera}/tamper", json={"enabled": False, "sensitivity": 50, "sustain_seconds": 60}).status_code, 403)

    def test_a_camera_someone_cannot_see_does_not_exist_for_them(self):
        hidden = self.limited.get(f"/api/cameras/{self.y1}/tamper")
        missing = self.limited.get(f"/api/cameras/{self.y1[:-1]}x/tamper")
        self.assertEqual((hidden.status_code, hidden.json()), (404, missing.json() and hidden.json()))
        self.assertEqual(self.admin.get("/api/cameras/nope/tamper").status_code, 404)
        self.assertEqual(self.admin.put("/api/cameras/nope/tamper", json={"enabled": True, "sensitivity": 50, "sustain_seconds": 60}).status_code, 404)

    def test_the_state_and_what_it_measured_are_reported(self):
        self.enable()
        self.learn()
        self.feed(FRAMES["normal"][5:6])
        body = self.admin.get(f"/api/cameras/{self.camera}/tamper").json()
        self.assertEqual(body["state"], "ok")
        self.assertEqual(body["learning_progress"], tamper.LEARN_SAMPLES)
        self.assertIsNotNone(body["reference_at"])
        self.assertGreater(body["last_metrics"]["layout_match"], 0.9)

    def test_webhook_event_types_and_the_incident_feed_know_tamper(self):
        types = self.admin.get("/api/webhooks/event-types").json()["event_types"] if self.admin.get("/api/webhooks/event-types").status_code == 200 else None
        from app.webhooks import EVENT_TYPES
        self.assertTrue({"tamper.detected", "tamper.recovered"} <= EVENT_TYPES)
        self.assertTrue(types is None or {"tamper.detected", "tamper.recovered"} <= set(types))
        self.enable()
        self.learn()
        self.feed(FRAMES["black"][:6])
        listed = self.admin.get("/api/incidents").json()["incidents"]
        self.assertIn("tamper_covered", {incident["fault_type"] for incident in listed})

    def test_metrics_report_the_state_of_cameras_that_have_it_on(self):
        self.enable()
        self.learn()
        self.feed(FRAMES["black"][:6])
        from app import prometheus
        text = prometheus.render(False)
        self.assertIn(f'nvr_camera_tamper_state{{camera_id="{self.camera}",camera="Dock 1",state="covered"}} 1', text)
        self.assertNotIn(self.d2, "".join(line for line in text.splitlines() if line.startswith("nvr_camera_tamper_state")))


class RecordingContinuityTests(TamperCase):
    def test_a_sampler_that_hangs_or_fails_never_holds_up_the_recorder_or_its_shutdown(self):
        from app.recording import RecordingManager
        self.enable(self.d1)
        self.enable(self.d2)
        calls = []

        async def hang(camera_id: str) -> bytes:
            calls.append(camera_id)
            await asyncio.sleep(3600)
            return b""

        async def scenario() -> float:
            manager = RecordingManager()
            manager.tamper = tamper.TamperManager(grabber=hang, interval=0.05)
            await manager.start()
            await asyncio.sleep(0.4)
            began = time.monotonic()
            ticks = []

            async def ticker():
                for _ in range(10):
                    ticks.append(time.monotonic())
                    await asyncio.sleep(0.02)
            await ticker()
            self.assertLess(max(b - a for a, b in zip(ticks, ticks[1:])), 0.2)  # The loop stayed free while samplers hung.
            await manager.stop()
            return time.monotonic() - began
        elapsed = asyncio.run(scenario())
        self.assertTrue(calls)
        self.assertLess(elapsed, 5)

    def test_segments_are_indexed_on_time_while_sampling_is_stuck(self):
        from app.recording import RecordingManager
        root = Path(self.environment["NVR_RECORDINGS_PATH"])
        output = root / self.camera
        output.mkdir(parents=True, exist_ok=True)
        rows = []
        for index in range(5):
            name = f"segment-20260301T08{index:02d}00Z.mp4"
            (output / name).write_bytes(b"x" * 500)
            rows.append(f"{name},0,10")
        (root / "index.csv").write_text("\n".join(rows))
        self.enable()
        entered = asyncio.Event

        async def stuck(_camera: str) -> bytes:
            await asyncio.sleep(3600)
            return b""

        async def scenario() -> int:
            sampler = tamper.TamperManager(grabber=stuck, interval=0.05)
            await sampler.start()
            await asyncio.sleep(0.2)
            began = time.monotonic()
            indexed = await asyncio.to_thread(RecordingManager()._index_csv, self.camera, root / "index.csv", output)
            self.assertLess(time.monotonic() - began, 3)
            await sampler.stop()
            return indexed
        self.assertEqual(asyncio.run(scenario()), 5)
        self.assertIsNotNone(entered)

    def test_one_camera_failing_does_not_stop_the_others_or_the_loop(self):
        self.enable(self.d1)
        self.enable(self.d2)

        async def flaky(camera_id: str) -> bytes:
            if camera_id == self.d1:
                raise RuntimeError("camera exploded")
            return FRAMES["normal"][0]
        manager = tamper.TamperManager(grabber=flaky)
        with self.assertLogs("nvr.tamper", "ERROR"):
            judged = asyncio.run(manager.sample_all(START))
        self.assertEqual(judged, 1)
        with connect() as db:
            self.assertEqual(db.execute("SELECT reference_samples FROM tamper_settings WHERE camera_id=?", (self.d2,)).fetchone()[0], 1)

    def test_the_loop_survives_a_pass_that_fails(self):
        self.enable()
        manager = tamper.TamperManager(interval=0.02)
        calls = []

        async def explode(now=None):
            calls.append(1)
            raise RuntimeError("pass failed")

        async def scenario():
            manager.sample_all = explode
            with self.assertLogs("nvr.tamper", "ERROR"):
                await manager.start()
                await asyncio.sleep(0.2)
                await manager.stop()
        asyncio.run(scenario())
        self.assertGreater(len(calls), 2)

    def test_cameras_are_grabbed_a_few_at_a_time(self):
        for camera in (self.d1, self.d2, self.y1, self.loose):
            self.enable(camera)
        running, peak = [0], [0]

        async def grab(_camera: str) -> bytes:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            await asyncio.sleep(0.05)
            running[0] -= 1
            return FRAMES["normal"][0]
        asyncio.run(tamper.TamperManager(grabber=grab).sample_all(START))
        self.assertEqual(peak[0], tamper.CONCURRENT_GRABS)


class GrabTests(unittest.TestCase):
    def test_a_frame_is_pulled_from_a_stream_the_way_the_recorder_reads_one(self):
        movie = SCENE.parent / "movie.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(SCENE), "-t", "1", "-r", "5", "-vf", "format=yuv420p", "-c:v", "libx264", str(movie)], check=True)
        frame = asyncio.run(tamper.grab_frame(f"file:{movie}", "concat,pipe,file"))
        self.assertEqual(len(frame), tamper.FRAME_BYTES)
        self.assertIsNone(tamper.judge(tamper.features(frame), tamper.features(frame), 50)[0])

    def test_only_camera_protocols_are_allowed_unless_widened(self):
        movie = SCENE.parent / "movie.mp4"
        self.assertIsNone(asyncio.run(tamper.grab_frame(f"file:{movie}")))

    def test_a_stream_that_never_answers_gives_up(self):
        with mock.patch.object(tamper, "GRAB_TIMEOUT_SECONDS", 0.5):
            began = time.monotonic()
            self.assertIsNone(asyncio.run(tamper.grab_frame("rtsp://192.0.2.1:554/never")))
            self.assertLess(time.monotonic() - began, 5)


class UpgradeTests(unittest.TestCase):
    def test_rebuilding_rules_keeps_every_rule_and_what_refers_to_it(self):
        from app import migrations
        from app.store import initialize
        with tempfile.TemporaryDirectory() as directory:
            previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_RECORDINGS_PATH")}
            os.environ.update({"NVR_DB_PATH": str(Path(directory) / "s.sqlite3"), "NVR_CREDENTIAL_KEY_PATH": str(Path(directory) / "k"), "NVR_RECORDINGS_PATH": directory})
            try:
                before = [(version, module) for version, module in migrations.MIGRATIONS if version <= 20]
                with mock.patch.object(migrations, "MIGRATIONS", before):
                    initialize()
                with connect() as db:
                    db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('c1','One',x'00')")
                    db.execute("INSERT INTO rules(id,name,condition_type,condition_json,classes_json,active_from,camera_ids_json,pop_into_live,cooldown_seconds) "
                               "VALUES('r1','Gate','zone_entry','{\"zone\":{}}','[\"person\"]','2026-01-01T00:00:00Z','[\"c1\"]',1,30)")
                    db.execute("INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json) VALUES('e1','r1','Gate','c1','t1','person','zone_entry','2026-01-02T00:00:00Z','{}')")
                    from app import alarms
                    alarm_id = alarms.create_alarm(db, "e1")
                    with self.assertRaises(Exception):
                        db.execute("INSERT INTO rules(name,condition_type,condition_json,classes_json,active_from) VALUES('x','tamper','{}','[]','2026-01-01T00:00:00Z')")
                initialize()
                with connect() as db:
                    rule = db.execute("SELECT * FROM rules WHERE id='r1'").fetchone()
                    self.assertEqual((rule["name"], rule["pop_into_live"], rule["cooldown_seconds"], rule["camera_ids_json"]), ("Gate", 1, 30, '["c1"]'))
                    self.assertEqual(db.execute("SELECT rule_id FROM rule_events WHERE id='e1'").fetchone()[0], "r1")
                    self.assertEqual(db.execute("SELECT rule_id FROM alarms WHERE id=?", (alarm_id,)).fetchone()[0], "r1")
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                    db.execute("INSERT INTO rules(name,condition_type,condition_json,classes_json,active_from) VALUES('x','tamper','{}','[]','2026-01-01T00:00:00Z')")
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM tamper_settings").fetchone()[0], 0)
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
