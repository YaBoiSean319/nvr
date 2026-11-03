"""Fall detection (#230): what counts as a fall, on synthetic body-point sequences (the pose model itself is tested with the model, see test_pose_models)."""
from __future__ import annotations

import math
import unittest

from app import falls

ASPECT = 16 / 9
NAMES = ("left_shoulder", "right_shoulder", "left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle")


def pose(angle: float, origin=(0.5, 0.3), visible: float = 0.9, aspect: float = ASPECT) -> dict[str, tuple[float, float, float]]:
    """A body whose torso leans ``angle`` degrees from upright on the screen (0 standing, 90 lying), legs continuing the line of the torso."""
    direction = (math.sin(math.radians(angle)) / aspect, math.cos(math.radians(angle)))
    at = lambda distance: (origin[0] + direction[0] * distance, origin[1] + direction[1] * distance, visible)
    return {"left_shoulder": at(0), "right_shoulder": at(0), "left_hip": at(0.20), "right_hip": at(0.20), "left_knee": at(0.38), "right_knee": at(0.38), "left_ankle": at(0.56), "right_ankle": at(0.56)}


def run(detector: falls.FallDetector, steps, start: float = 0.0, step: float = 0.5, track: str = "t1"):
    """Feed ``steps`` (an angle, or None for 'cannot be made out') one every ``step`` seconds; return the falls raised, with when."""
    raised = []
    for index, angle in enumerate(steps):
        now = start + index * step
        found = detector.update(track, now, None if angle is None else pose(angle))
        if found:
            raised.append((round(now, 1), found))
    return raised


class GeometryTests(unittest.TestCase):
    def test_the_torso_angle_is_zero_standing_and_ninety_lying(self):
        self.assertAlmostEqual(falls.torso_angle(pose(0), ASPECT), 0, delta=0.5)
        self.assertAlmostEqual(falls.torso_angle(pose(45), ASPECT), 45, delta=0.5)
        self.assertAlmostEqual(falls.torso_angle(pose(90), ASPECT), 90, delta=0.5)

    def test_a_torso_that_cannot_be_made_out_has_no_angle(self):
        self.assertIsNone(falls.torso_angle({}, ASPECT))
        hidden = pose(0, visible=0.05)
        self.assertIsNone(falls.torso_angle(hidden, ASPECT))

    def test_a_person_lying_is_wider_than_tall_and_one_standing_is_not(self):
        self.assertGreater(falls.body_shape(pose(90), ASPECT), 1.3)
        self.assertLess(falls.body_shape(pose(0), ASPECT), 0.3)


class FallTests(unittest.TestCase):
    def detector(self, **kwargs) -> falls.FallDetector:
        return falls.FallDetector(aspect=ASPECT, stay_down_seconds=3.0, **kwargs)

    def test_a_person_who_falls_and_stays_down_raises_one_fall_after_the_stay_down_time(self):
        walking, down = [5, 8, 5, 6, 4, 7], [85] * 14
        raised = run(self.detector(), walking + down)
        self.assertEqual(len(raised), 1)
        when, fall = raised[0]
        self.assertEqual(when, 3.0 + 3.0)  # Went low at 3.0 s (index 6), raised once 3 s later
        self.assertGreaterEqual(fall.down_seconds, 3.0)

    def test_a_stumble_that_recovers_raises_nothing(self):
        self.assertEqual(run(self.detector(), [5, 5, 80, 80, 80, 5, 5, 5, 5]), [])  # Down for 1.5 s, then up

    def test_bending_over_raises_nothing(self):
        self.assertEqual(run(self.detector(), [5, 5, 55, 60, 55, 5, 5, 5]), [])  # Torso past 50 degrees for 1 s only

    def test_sitting_raises_nothing(self):
        self.assertEqual(run(self.detector(), [5, 5, 20, 25, 25, 25, 25, 25, 25, 25, 25]), [])

    def test_a_person_first_seen_lying_raises_nothing(self):
        # A resident in bed: never seen upright, so never a fall, however long they lie there.
        self.assertEqual(run(self.detector(), [88] * 40), [])

    def test_lying_down_slowly_raises_nothing(self):
        # Upright, then a long time between upright and low (a person lying down on a sofa): more than the transition time.
        raised = run(self.detector(), [5, 5] + [30, 36, 40, 42, 44, 45, 46, 47, 47, 48, 48, 49, 49, 49] + [88] * 14)  # Ten seconds between upright and flat
        self.assertEqual(raised, [])

    def test_a_person_hidden_after_going_down_still_raises_the_fall(self):
        # Falls behind a bed: low for a moment, then the pose cannot be made out. Time passes with the camera still following them.
        detector = self.detector()
        self.assertEqual(run(detector, [5, 5, 5, 85]), [])
        raised = []
        for second in range(2, 8):
            raised += detector.tick(1.5 + second, set())
        self.assertEqual(len(raised), 1)

    def test_a_person_unseen_for_too_long_is_forgotten_not_alerted(self):
        detector = self.detector()
        run(detector, [5, 5, 85])
        self.assertEqual(detector.tick(60.0, set()), [])  # Hidden for a minute: no alert from a stale 'down'

    def test_one_fall_raises_one_event_and_a_second_needs_them_to_be_upright_in_between(self):
        detector = self.detector()
        first = run(detector, [5, 5, 85] + [85] * 10)
        self.assertEqual(len(first), 1)
        still_down = run(detector, [85] * 10, start=10.0)
        self.assertEqual(still_down, [])
        up_then_down = run(detector, [5] * 6 + [85] * 10, start=20.0)  # Up for 3 s, falls again
        self.assertEqual(len(up_then_down), 1)

    def test_people_are_followed_separately(self):
        detector = self.detector()
        raised = []
        for index in range(20):
            now = index * 0.5
            standing = detector.update("walker", now, pose(5))
            faller = detector.update("faller", now, pose(5 if index < 4 else 85))
            raised += [(item.track_id, now) for item in (standing, faller) if item]
        self.assertEqual([track for track, _ in raised], ["faller"])

    def test_low_sensitivity_ignores_a_tilt_that_high_sensitivity_alerts_on(self):
        # A torso 55 degrees from upright: "low" on high sensitivity (past 50), not on low (past 70).
        steps = [5, 5, 5] + [55] * 14
        self.assertEqual(len(run(self.detector(sensitivity="high"), steps)), 1)
        self.assertEqual(run(self.detector(sensitivity="low"), steps), [])

    def test_a_longer_stay_down_time_waits_longer(self):
        steps = [5, 5, 5] + [85] * 20
        short = run(falls.FallDetector(aspect=ASPECT, stay_down_seconds=2.0), steps)
        long = run(falls.FallDetector(aspect=ASPECT, stay_down_seconds=8.0), steps)
        self.assertEqual((short[0][0], long[0][0]), (3.5, 9.5))

    def test_a_person_in_between_poses_after_going_down_is_still_down(self):
        # Down, then a torso at 45 degrees (neither upright nor low: sitting up on the floor), then down again: the fall is not forgotten.
        raised = run(self.detector(), [5, 5, 85, 45, 45, 45, 85, 85, 85, 85])
        self.assertEqual(len(raised), 1)

    def test_a_person_unseen_for_a_while_then_found_low_still_raises_the_fall(self):
        detector = self.detector()
        run(detector, [5, 5, 85])
        raised = run(detector, [None] * 8 + [85], start=2.0)  # Hidden for four seconds, then seen low again
        self.assertEqual(len(raised), 1)

    def test_a_fallen_person_the_tracker_loses_and_finds_again_is_still_one_fall(self):
        # The tracker follows the person to the floor, drops them, and starts a new track where they lie: the new track is the same person.
        detector = self.detector()
        for index, angle in enumerate([5, 5, 85]):
            detector.update("old", index * 0.5, pose(angle), centre=(0.4, 0.5))
        detector.forget(set(), now=1.0)  # The tracker has lost them
        raised = []
        for index in range(12):
            found = detector.update("new", 1.5 + index * 0.5, pose(88), centre=(0.42, 0.55))
            if found:
                raised.append(found)
        self.assertEqual(len(raised), 1)

    def test_a_new_track_lying_far_from_anyone_just_lost_is_not_a_fall(self):
        detector = self.detector()
        for index, angle in enumerate([5, 5, 5]):
            detector.update("walker", index * 0.5, pose(angle), centre=(0.1, 0.5))
        detector.forget(set(), now=1.0)
        raised = [detector.update("bed", 1.5 + index * 0.5, pose(88), centre=(0.9, 0.5)) for index in range(12)]
        self.assertEqual([item for item in raised if item], [])  # A resident in a bed across the room, nowhere near

    def test_someone_standing_beside_a_bed_does_not_make_the_person_in_it_a_fall(self):
        detector = self.detector()
        raised = []
        for index in range(14):
            now = index * 0.5
            detector.update("carer", now, pose(5), centre=(0.5, 0.5))  # Still being followed, upright, right by the bed
            found = detector.update("resident", now, pose(88), centre=(0.52, 0.55))
            if found:
                raised.append(found)
        self.assertEqual(raised, [])

    def test_a_lost_person_is_forgotten_after_a_while(self):
        detector = self.detector()
        for index in range(3):
            detector.update("old", index * 0.5, pose(5 if index < 2 else 85), centre=(0.4, 0.5))
        detector.forget(set(), now=1.0)
        later = [detector.update("new", 30.0 + index * 0.5, pose(88), centre=(0.4, 0.5)) for index in range(12)]
        self.assertEqual([item for item in later if item], [])  # Half a minute on, a new track lying there is not the person who fell

    def test_tracks_that_have_gone_are_forgotten(self):
        detector = self.detector()
        run(detector, [5, 5], track="gone")
        run(detector, [5, 5], track="here")
        detector.forget({"here"})
        self.assertEqual(set(detector.tracks), {"here"})


if __name__ == "__main__":
    unittest.main()
