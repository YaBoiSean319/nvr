import threading
import unittest
from unittest.mock import patch

from app import detection
from tests.scene_case import SceneTestCase


class AnalysisKeepsTheEventLoopFreeTests(SceneTestCase):
    def test_the_frame_database_writes_run_off_the_event_loop(self):
        main = threading.get_ident()
        where: dict[str, set[bool]] = {"state": set(), "observations": set(), "tracks": set()}
        real_state, real_observations = detection.set_camera_state, detection.record_observations

        def state(*args, **kwargs):
            if args[1:2] == ("analyzed",):
                where["state"].add(threading.get_ident() == main)
            return real_state(*args, **kwargs)

        def observations(*args, **kwargs):
            where["observations"].add(threading.get_ident() == main)
            return real_observations(*args, **kwargs)

        from app import tracks
        real_tracks = tracks.record_detections

        def stored(*args, **kwargs):
            where["tracks"].add(threading.get_ident() == main)
            return real_tracks(*args, **kwargs)

        with patch.object(detection, "set_camera_state", state), patch.object(detection, "record_observations", observations), patch.object(tracks, "record_detections", stored):
            self.analyse_footage()
        self.assertEqual(where, {"state": {False}, "observations": {False}, "tracks": {False}})  # Written, and never on the loop's thread


if __name__ == "__main__":
    unittest.main()
