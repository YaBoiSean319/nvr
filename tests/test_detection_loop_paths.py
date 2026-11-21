"""The paths of the AI worker's camera loop that no other test runs, pinned before it is split (#286).

``DetectionManager._run`` (app/detection.py, complexity F 69) is already run over the fixture footage by about twenty test modules (tracks,
falls, plates, privacy, search, rules). Measured with coverage, what none of them reached was: a camera waiting for the inference slot,
the face watch's scanner as the loop feeds it, a fall setting changed while the camera is analysed, an object's appearance or image-text
embedding failing, and the loop stopping in the middle of a stream. These run the real loop over the same footage and pin each.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable
from unittest.mock import patch

from app import detection, face_scan, falls
from app.detection import DetectionManager
from app.store import connect
from tests.fixtures import detection_scene as scene
from tests.scene_case import SceneTestCase


class LoopPathTests(SceneTestCase):
    def run_loop(self, prepare: Callable[[DetectionManager], Any] | None = None, *, frames: int | None = None, during: Callable[[DetectionManager, int], Any] | None = None) -> DetectionManager:
        """Run the AI worker's loop for this camera over the fixture footage (or its first ``frames``); ``during`` is called before each frame."""
        decoded = scene.decode_footage(self.footage)[:frames]

        async def run() -> DetectionManager:
            finished = asyncio.Event()
            manager = DetectionManager()
            manager.detector = scene.SceneDetector()

            async def footage_frames(source, fps):
                for number, item in enumerate(decoded):
                    if during:
                        await during(manager, number)
                    yield item
                finished.set()
                await asyncio.Event().wait()

            if prepare:
                prepare(manager)
            with patch("app.detection._raw_frames", footage_frames):
                await manager.start()
                await asyncio.wait_for(finished.wait(), timeout=30)
                await asyncio.sleep(0.2)
                await manager.stop()
            return manager

        return asyncio.run(run())

    def states(self) -> list[tuple[str, str | None]]:
        return self.recorded_states

    def record_states(self) -> None:
        self.recorded_states: list[tuple[str, str | None]] = []
        real = detection.set_camera_state

        def recording(camera_id, state, error=None, at=None):
            self.recorded_states.append((state, error))
            return real(camera_id, state, error, at)

        patcher = patch.object(detection, "set_camera_state", recording)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tracks(self) -> int:
        with connect() as db:
            return db.execute("SELECT COUNT(*) FROM ai_tracks WHERE camera_id=?", (self.camera_id,)).fetchone()[0]

    # --- waiting for the inference slot ----------------------------------------------------------------------------------------------

    def analysed_at(self, fps: float) -> None:
        # A frame counts as delayed after waiting max(1 s, two frame intervals) for the slot: at 4 frames a second, after 1 s.
        with connect() as db:
            db.execute("UPDATE ai_camera_settings SET analysis_fps=? WHERE camera_id=?", (fps, self.camera_id))

    def hold_the_slot_before(self, frame: int, seconds: float):
        async def during(manager, number):
            if number == frame:
                async def hold():
                    async with manager._inference_slots:
                        await asyncio.sleep(seconds)
                manager._holder = asyncio.create_task(hold())
                await asyncio.sleep(0)
        return during

    def test_a_camera_kept_waiting_for_the_inference_slot_is_marked_delayed_but_not_late_while_warming_up(self):
        self.record_states()
        self.analysed_at(4.0)
        late: list[str] = []
        self.run_loop(lambda manager: setattr(manager.capacity, "note_late", late.append), frames=3, during=self.hold_the_slot_before(0, 1.3))
        self.assertIn(("delayed", "Inference was delayed while CPU capacity was occupied"), self.states())
        self.assertEqual(late, [])  # The first frames wait behind other cameras' model start-up: not a sign of too little capacity
        self.assertGreater(self.tracks(), 0)  # The frame was still analysed after the wait

    def test_after_warming_up_a_camera_kept_waiting_is_counted_as_late(self):
        self.record_states()
        self.analysed_at(4.0)
        late: list[str] = []
        self.run_loop(lambda manager: setattr(manager.capacity, "note_late", late.append), frames=detection.WARM_UP_FRAMES + 2,
                      during=self.hold_the_slot_before(detection.WARM_UP_FRAMES, 1.3))
        self.assertEqual(late, [self.camera_id])
        self.assertIn(("delayed", "Inference was delayed while CPU capacity was occupied"), self.states())

    # --- the face watch ----------------------------------------------------------------------------------------------------------------

    def test_the_face_watch_scanner_is_made_once_fed_each_person_and_told_a_new_threshold(self):
        made: list[Any] = []

        class Scanner:
            def __init__(self, reader, snapshot, watched, store, *, threshold):
                self.reader, self.threshold, self.seen_items, self.forgotten = reader, threshold, [], 0
                made.append(self)

            def seen(self, track_id, label, box):
                self.seen_items.append((track_id, label))

            def forget(self, active):
                self.forgotten += 1

            def due(self):
                return False

        thresholds = iter([0.4] + [0.55] * 1000)
        reader = object()

        async def face_reader(provider):
            return reader

        with patch.object(face_scan, "FaceScanner", Scanner), patch.object(face_scan, "SETTINGS_REFRESH", 0.0), \
                patch.object(face_scan, "scanning", lambda db, camera_id: (True, next(thresholds))):
            self.run_loop(lambda manager: setattr(manager, "_face_reader", face_reader))
        self.assertEqual(len(made), 1)  # Made once, while the reader stays the same
        (scanner,) = made
        self.assertIs(scanner.reader, reader)
        self.assertEqual(scanner.threshold, 0.55)  # A changed threshold reaches the running scanner
        self.assertTrue(scanner.seen_items)
        self.assertIn("person", {label for _, label in scanner.seen_items})  # Fed each tracked object with its label; it picks the people itself
        self.assertEqual(scanner.forgotten, scene.FRAME_COUNT)  # Told which tracks are still active after every frame

    def test_a_face_scanner_with_a_read_due_has_it_done_in_the_background(self):
        scans: list[str] = []

        class Scanner:
            def __init__(self, *args, threshold):
                self.asked = 0

            def seen(self, track_id, label, box):
                pass

            def forget(self, active):
                pass

            def due(self):
                self.asked += 1
                return self.asked == 2  # One read, due on the second frame

            async def scan(self):
                await asyncio.sleep(0.05)  # Longer than a frame: analysis goes on meanwhile
                scans.append(asyncio.current_task().get_name())

        async def face_reader(provider):
            return object()

        with patch.object(face_scan, "FaceScanner", Scanner), patch.object(face_scan, "scanning", lambda db, camera_id: (True, 0.5)):
            self.run_loop(lambda manager: setattr(manager, "_face_reader", face_reader))
        self.assertEqual(scans, [f"faces-{self.camera_id}"])

    def test_without_the_face_watch_no_scanner_is_made(self):
        made: list[Any] = []
        with patch.object(face_scan, "FaceScanner", lambda *args, **kwargs: made.append(args)), patch.object(face_scan, "SETTINGS_REFRESH", 0.0), \
                patch.object(face_scan, "scanning", lambda db, camera_id: (False, 0.5)):
            self.run_loop()
        self.assertEqual(made, [])

    # --- fall settings changed while analysing -------------------------------------------------------------------------------------

    def test_a_fall_setting_changed_while_analysing_reaches_the_running_detector(self):
        made: list[Any] = []

        class Detector:
            def __init__(self, *, sensitivity, stay_down_seconds, aspect):
                self.sensitivity, self.stay_down_seconds, self.aspect = sensitivity, stay_down_seconds, aspect
                made.append(self)

            def update(self, track_id, moment, pose, centre):
                return None

            def tick(self, moment, present):
                return []

            def forget(self, active, moment):
                return None

        class Reader:
            def pose_full(self, picture, box):
                return None

        async def pose_reader(provider):
            return Reader()

        settings = iter([(True, "low", 3.0, 1.5, [])] + [(True, "high", 8.0, 1.5, [])] * 1000)
        with patch.object(falls, "FallDetector", Detector), patch.object(falls, "SETTINGS_REFRESH", 0.0), \
                patch.object(detection, "_fall_refresh", lambda camera_id, at: next(settings)):
            self.run_loop(lambda manager: setattr(manager, "_pose_reader", pose_reader))
        self.assertEqual(len(made), 1)
        self.assertEqual((made[0].sensitivity, made[0].stay_down_seconds), ("high", 8.0))

    # --- embeddings never stop detection -----------------------------------------------------------------------------------------------

    def test_an_appearance_or_image_text_embedding_that_fails_never_stops_detection(self):
        self.record_states()

        class Clip:
            model_id = "test-clip"

            def embed_image(self, frame, box):
                raise RuntimeError("the image-text model failed")

        async def clip_model():
            return Clip()

        def broken(*args, **kwargs):
            raise RuntimeError("the appearance model failed")

        with patch("app.similar.embed", broken):
            self.run_loop(lambda manager: setattr(manager, "_clip_model", clip_model))
        self.assertGreater(self.tracks(), 0)
        with connect() as db:
            keyframes = db.execute("SELECT COUNT(*) FROM track_keyframes k JOIN ai_tracks t ON t.id=k.track_id WHERE t.camera_id=?", (self.camera_id,)).fetchone()[0]
        self.assertGreater(keyframes, 0)  # Each object's keyframe was still kept
        self.assertNotIn("failed", [state for state, _ in self.states()])

    # --- memory over a long stream ------------------------------------------------------------------------------------------------------

    def test_objects_that_have_left_are_not_remembered_for_the_rest_of_the_stream(self):
        # Each object's first frame is kept as its keyframe, once: the loop remembered every object it had keyframed for as long as the
        # camera's stream stayed open. A busy camera sees thousands of objects a day and a stream can stay open for weeks (#217).
        from datetime import datetime, timedelta, timezone

        seen: list[set[str]] = []
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        ticks = iter(range(10_000))

        def prepare(manager):
            manager.clock = lambda: start + timedelta(seconds=30 * next(ticks))  # Every object has left before the next frame: each is new
            real = manager._keep_first_sightings

            async def watched(frame, at, track_ids, observations, keyframed, models):
                if not seen:
                    seen.append(keyframed)
                await real(frame, at, track_ids, observations, keyframed, models)

            manager._keep_first_sightings = watched

        self.run_loop(prepare)
        self.assertGreater(self.tracks(), len(scene.OBJECTS) * 3)  # Many objects came and went
        self.assertLessEqual(len(seen[0]), len(scene.OBJECTS))  # Only those still in view are remembered

    # --- stopping ----------------------------------------------------------------------------------------------------------------------

    def test_stopping_in_the_middle_of_a_stream_ends_the_loop_without_calling_the_camera_failed(self):
        self.record_states()

        async def during(manager, number):
            if number == 3:
                manager.stopping = True

        async def run() -> None:
            decoded = scene.decode_footage(self.footage)
            manager = DetectionManager()
            manager.detector = scene.SceneDetector()
            done = asyncio.Event()

            async def footage_frames(source, fps):
                for number, item in enumerate(decoded):
                    await during(manager, number)
                    yield item
                await asyncio.Event().wait()

            with patch("app.detection._raw_frames", footage_frames):
                original = manager._run

                async def watched(*args):
                    try:
                        await original(*args)
                    finally:
                        done.set()

                manager._run = watched
                await manager.start()
                await asyncio.wait_for(done.wait(), timeout=30)  # The loop returned on its own, without being cancelled
                await manager.stop()

        asyncio.run(run())
        self.assertNotIn("failed", [state for state, _ in self.states()])
