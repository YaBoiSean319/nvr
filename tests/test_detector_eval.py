"""The person-detector evaluation tool (#255): scoring of found and missed people, and reading pictures the way the worker does."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app import detector_eval as ev
from app.detection import MODEL_FRAME_BYTES


def box(x: float, y: float = 0.3, w: float = 0.1, h: float = 0.4) -> dict[str, float]:
    return {"x": x, "y": y, "width": w, "height": h}


def person(x: float, confidence: float) -> dict:
    return {"label": "person", "confidence": confidence, "box": box(x)}


class LabelTests(unittest.TestCase):
    def write(self, content) -> Path:
        folder = Path(tempfile.mkdtemp())
        path = folder / "labels.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        return path

    def test_labels_are_read_with_a_tag_that_defaults_to_person(self):
        labelled = ev.load_labels(self.write({"frames": {"a.jpg": [{"box": [0.1, 0.2, 0.3, 0.4]}, {"box": [0.5, 0.2, 0.1, 0.3], "tag": "wheelchair"}], "b.jpg": []}}))
        self.assertEqual([truth.tag for truth in labelled["a.jpg"]], ["person", "wheelchair"])
        self.assertEqual(labelled["a.jpg"][0].box, {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4})
        self.assertEqual(labelled["b.jpg"], [])  # A picture with nobody in it is a test of false alarms

    def test_bad_labels_are_refused_with_the_picture_named(self):
        for content in ({}, {"frames": {}}, {"frames": {"a.jpg": [{"box": [0.1, 0.2]}]}}, {"frames": {"a.jpg": [{"box": [0.1, 0.2, 0, 0.4]}]}}, {"frames": {"a.jpg": [{"tag": "x"}]}}):
            with self.subTest(content=content), self.assertRaises(ValueError):
                ev.load_labels(self.write(content))


class ScoringTests(unittest.TestCase):
    def test_a_person_is_found_when_a_confident_enough_box_overlaps_them(self):
        truths = [ev.Truth(box(0.1), "person")]
        self.assertEqual(ev.match([person(0.1, 0.6)], truths, 0.55, 0.5), ([True], 0))
        self.assertEqual(ev.match([person(0.1, 0.5)], truths, 0.55, 0.5), ([False], 0))  # Seen, but not confident enough
        self.assertEqual(ev.match([person(0.6, 0.9)], truths, 0.55, 0.5), ([False], 1))  # A box on nobody is a false alarm
        self.assertEqual(ev.match([person(0.1, 0.9), person(0.1, 0.8)], truths, 0.55, 0.5), ([True], 1))  # One box finds one person; the second is a false alarm
        self.assertEqual(ev.match([{"label": "vehicle", "confidence": 0.9, "box": box(0.1)}], truths, 0.1, 0.5), ([False], 0))  # Vehicles are not people

    def test_the_best_score_says_whether_a_lower_threshold_would_have_found_them(self):
        truths = [ev.Truth(box(0.1), "wheelchair"), ev.Truth(box(0.6), "person")]
        scores = ev.best_scores([person(0.1, 0.42), person(0.1, 0.2)], truths, 0.5)
        self.assertEqual(scores, [0.42, None])  # The first: scored 0.42. The second: the model saw nothing there at all

    def test_rows_count_found_false_alarms_and_each_tag_at_every_threshold(self):
        frames = {
            "a.jpg": ([ev.Truth(box(0.1), "person"), ev.Truth(box(0.6), "wheelchair")], [person(0.1, 0.9), person(0.6, 0.45), person(0.3, 0.5)]),
            "b.jpg": ([], [person(0.2, 0.7)]),
        }
        low, high = ev.evaluate(frames, (0.4, 0.6))
        self.assertEqual((low.people, low.found, low.false_alarms, low.frames), (2, 2, 2, 2))
        self.assertEqual(low.by_tag, {"person": (1, 1), "wheelchair": (1, 1)})
        self.assertEqual((high.found, high.false_alarms), (1, 1))
        self.assertEqual(high.by_tag, {"person": (1, 1), "wheelchair": (0, 1)})
        self.assertAlmostEqual(high.recall, 0.5)

    def test_misses_list_every_person_not_found_with_the_model_s_best_confidence(self):
        frames = {"a.jpg": ([ev.Truth(box(0.1), "wheelchair"), ev.Truth(box(0.6), "person")], [person(0.1, 0.42)])}
        missed = ev.misses(frames, 0.55)
        self.assertEqual([(item["tag"], item["best_score"]) for item in missed], [("wheelchair", 0.42), ("person", None)])

    def test_the_report_says_what_a_lower_threshold_would_and_would_not_fix(self):
        frames = {"a.jpg": ([ev.Truth(box(0.1), "wheelchair"), ev.Truth(box(0.6), "person")], [person(0.1, 0.42)])}
        text = ev.report(ev.evaluate(frames, (0.4, 0.55)), ev.misses(frames, 0.55), 0.55, 80.0)
        self.assertIn("2 people on 1 frames, 80 ms a frame", text)
        self.assertIn("1 of 2 misses were scored below the threshold", text)
        self.assertIn("1 were not seen at all", text)
        self.assertIn("wheelchair", text)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
class PictureTests(unittest.TestCase):
    def test_pictures_are_pulled_from_a_recording_and_read_at_the_model_s_size(self):
        folder = Path(tempfile.mkdtemp())
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=5:d=10", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(folder / "clip.mp4")], check=True, timeout=60)
        self.assertEqual(ev.extract(folder / "clip.mp4", folder / "frames", every=2, limit=4), 4)
        names = sorted(path.name for path in (folder / "frames").iterdir())
        self.assertEqual(names, ["frame_00001.jpg", "frame_00002.jpg", "frame_00003.jpg", "frame_00004.jpg"])
        self.assertEqual(len(ev.decode_frame(folder / "frames" / names[0])), MODEL_FRAME_BYTES)
        labels = folder / "labels.json"
        labels.write_text(json.dumps({"frames": {name: [{"box": [0.1, 0.2, 0.2, 0.5], "tag": "wheelchair"}] for name in names}}), encoding="utf-8")
        seen = []

        def infer(frame: bytes, threshold: float):
            seen.append((len(frame), threshold))
            return [person(0.1, 0.5)]  # Always the same place, scored a little under the usual threshold

        text = ev.run_score(folder / "frames", labels, infer, (0.4, 0.55), 0.3)
        self.assertEqual(seen, [(MODEL_FRAME_BYTES, ev.LOW)] * 4)
        self.assertIn("4 people on 4 frames", text)
        self.assertIn("4 of 4 misses were scored below the threshold", text)

    def test_labels_for_pictures_that_are_not_there_are_reported_not_scored(self):
        folder = Path(tempfile.mkdtemp())
        labels = folder / "labels.json"
        labels.write_text(json.dumps({"frames": {"gone.jpg": []}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not in"):
            ev.run_score(folder, labels, lambda frame, threshold: [], (0.5,), 0.5)
        self.assertEqual(ev.main(["score", str(folder), str(labels)]), 1)  # Refused before any model is loaded


if __name__ == "__main__":
    unittest.main()
