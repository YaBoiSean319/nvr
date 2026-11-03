"""The fall evaluation tool (#230): matching raised falls to labels, and the whole run over a clip with a stand-in detector and pose reader."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app import fall_eval
from tests.fixtures import detection_scene as scene
from tests.test_fall_detection import ScriptedReader


class ScoreTests(unittest.TestCase):
    def test_a_raised_fall_matches_the_label_it_follows_within_the_window(self):
        scored = fall_eval.score([14.0], [10.0])
        self.assertEqual((scored.detected, scored.missed, scored.false_alarms), ([(10.0, 4.0)], [], []))

    def test_a_fall_raised_long_after_or_before_the_label_does_not_match(self):
        scored = fall_eval.score([60.0, 3.0], [20.0], tolerance=20.0)
        self.assertEqual((scored.detected, scored.missed, scored.false_alarms), ([], [20.0], [3.0, 60.0]))

    def test_each_label_takes_one_raised_fall_and_the_rest_are_false_alarms(self):
        scored = fall_eval.score([11.0, 12.0, 50.0], [10.0, 49.0])
        self.assertEqual(scored.detected, [(10.0, 1.0), (49.0, 1.0)])
        self.assertEqual(scored.false_alarms, [12.0])

    def test_a_clip_without_falls_makes_every_raised_fall_a_false_alarm(self):
        self.assertEqual(fall_eval.score([5.0], []).false_alarms, [5.0])
        self.assertEqual(fall_eval.score([], []).false_alarms, [])


class LabelTests(unittest.TestCase):
    def test_labels_name_clips_and_falls_and_refuse_nonsense(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.json"
            path.write_text(json.dumps({"clips": [{"file": "a.mp4", "falls": [9, 3.5], "note": "staged"}, {"file": "b.mp4"}]}))
            clips = fall_eval.load_labels(path)
            self.assertEqual([(clip.file.name, clip.falls, clip.note) for clip in clips], [("a.mp4", [3.5, 9.0], "staged"), ("b.mp4", [], "")])
            path.write_text(json.dumps({"clips": [{"file": "a.mp4", "falls": [-1]}]}))
            with self.assertRaises(ValueError):
                fall_eval.load_labels(path)
            path.write_text(json.dumps({"clips": []}))
            with self.assertRaises(ValueError):
                fall_eval.load_labels(path)


class RunTests(unittest.TestCase):
    """The whole worker path over the fixture scene (one person goes down and stays down), with stand-ins for the person detector and the pose model."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.clip = scene.write_footage(Path(cls.directory.name) / "scene.mp4")
        cls.frames = scene.decode_footage(cls.clip)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_clip(self) -> list[float]:
        async def frames(_source, _fps):
            for item in self.frames:
                yield item

        return fall_eval.run_clip(self.clip, sensitivity="high", stay_down=2.0, fps=2.0, detector=scene.SceneDetector(), reader=ScriptedReader(), frames=frames)

    def test_the_fall_in_the_scene_is_found_once_and_a_label_for_it_is_a_detection(self):
        raised = self.run_clip()
        self.assertEqual(len(raised), 1)
        self.assertTrue(0 < raised[0] < scene.FRAME_COUNT * 0.5 + 1)
        report = self.evaluate([fall_eval.Clip(self.clip, [raised[0] - 3.0])])
        self.assertEqual((report["totals"]["detected"], report["totals"]["missed"], report["totals"]["false_alarms"]), (1, 0, 0))
        self.assertEqual(report["totals"]["detection_rate"], 1.0)
        self.assertEqual(report["clips"][0]["detected"][0]["raised_after_seconds"], 3.0)

    def evaluate(self, clips):
        async def frames(_source, _fps):
            for item in self.frames:
                yield item

        return fall_eval.evaluate(clips, sensitivity="high", stay_down=2.0, fps=2.0, detector=scene.SceneDetector(), reader=ScriptedReader(), frames=frames, seconds=15.0)

    def test_an_unlabelled_fall_is_a_false_alarm_and_a_label_nothing_matches_is_a_miss(self):
        report = self.evaluate([fall_eval.Clip(self.clip, [])])
        self.assertEqual((report["totals"]["false_alarms"], report["totals"]["detected"]), (1, 0))
        self.assertIsNone(report["totals"]["detection_rate"])
        self.assertEqual(report["totals"]["false_alarms_per_camera_day"], round(1 / (15 / 86400), 2))
        report = self.evaluate([fall_eval.Clip(self.clip, [0.0])], )
        self.assertEqual(report["totals"]["missed"] + report["totals"]["detected"], 1)

    def test_the_text_report_says_what_was_found_and_what_was_not(self):
        report = self.evaluate([fall_eval.Clip(self.clip, [], "someone sat down")])
        text = fall_eval.render(report)
        self.assertIn("FALSE ALARM", text)
        self.assertIn("someone sat down", text)
        self.assertIn("not a safety system", text)

    def test_the_run_does_not_touch_the_environment_it_was_started_in(self):
        import os
        before = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")}
        self.run_clip()
        self.assertEqual({key: os.environ.get(key) for key in before}, before)


class AccuracyFixtureTests(unittest.TestCase):
    """The synthetic measurement behind the numbers in docs/fall-detection.md stays honest: what the docs say about it is what it computes."""

    def cell(self, sensitivity: str, stay_down: float, scenario: str, noise: float = 0.0, dropout: float = 0.0) -> float:
        from tests.fixtures import fall_accuracy

        rows = {name: share for name, _, share, _ in fall_accuracy.measure(sensitivity, stay_down, noise, dropout)}
        return rows[scenario]

    def test_falls_are_found_walking_and_sitting_never_raise_one(self):
        self.assertGreaterEqual(self.cell("high", 8.0, "Falls and stays down", 10.0), 0.97)
        self.assertEqual(self.cell("high", 8.0, "Walks about", 10.0, 0.2), 0.0)
        self.assertEqual(self.cell("high", 8.0, "Sits down on a chair", 10.0, 0.2), 0.0)

    def test_the_longer_stay_down_is_what_stops_alerts_for_bending_and_crouching(self):
        self.assertGreater(self.cell("high", 3.0, "Bends to pick something up", 5.0), 0.5)
        self.assertEqual(self.cell("high", 8.0, "Bends to pick something up", 10.0), 0.0)
        self.assertEqual(self.cell("high", 8.0, "Crouches to tie a shoe", 10.0), 0.0)

    def test_lying_down_on_purpose_is_a_known_gap_at_high_sensitivity(self):
        self.assertGreater(self.cell("high", 8.0, "Lies down slowly on a bed (12 s)", 5.0), 0.9)
        self.assertEqual(self.cell("high", 8.0, "Was already lying (in bed) when first seen", 5.0), 0.0)
