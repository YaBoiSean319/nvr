import threading
import unittest
from unittest.mock import patch

from app import detection
from tests.scene_case import SceneTestCase


class AnalysisKeepsTheEventLoopFreeTests(SceneTestCase):
    def test_the_frame_database_writes_run_off_the_event_loop(self):
        main = threading.get_ident()
        where: dict[str, set[bool]] = {"state": set(), "observations": set(), "tracks": set()}
        real_state, real_observations = detection._set_camera_state, detection._record_observations  # What _store_frame calls, on one connection (#279)

        def state(*args, **kwargs):
            if args[2:3] == ("analyzed",):  # (connection, camera, state, ...)
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

        with patch.object(detection, "_set_camera_state", state), patch.object(detection, "_record_observations", observations), patch.object(tracks, "record_detections", stored):
            self.analyse_footage()
        self.assertEqual(where, {"state": {False}, "observations": {False}, "tracks": {False}})  # Written, and never on the loop's thread

    def test_the_settings_refreshes_run_off_the_event_loop_too(self):
        # Plate and fall settings were read with a connection opened on the loop itself (#279): a busy database would stall the heartbeat.
        main = threading.get_ident()
        where: dict[str, set[bool]] = {"plates": set(), "falls": set()}
        real_plates, real_falls = detection._plate_refresh, detection._fall_refresh

        def plates(*args, **kwargs):
            where["plates"].add(threading.get_ident() == main)
            return real_plates(*args, **kwargs)

        def falls(*args, **kwargs):
            where["falls"].add(threading.get_ident() == main)
            return real_falls(*args, **kwargs)

        with patch.object(detection, "_plate_refresh", plates), patch.object(detection, "_fall_refresh", falls):
            self.analyse_footage()
        self.assertEqual(where, {"plates": {False}, "falls": {False}})


if __name__ == "__main__":
    unittest.main()
