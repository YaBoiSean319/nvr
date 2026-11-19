"""Small faults that show only when something else has already gone wrong (#386)."""
from __future__ import annotations

from tests.test_access_grants import GrantCase


class CsvImportTests(GrantCase):
    def test_a_row_whose_camera_name_is_only_spaces_is_reported_and_the_rest_of_the_import_goes_on(self):
        text = "name,stream_url\n   ,rtsp://192.0.2.10/live\nNew gate,rtsp://192.0.2.11/live\n"
        answer = self.admin.post("/api/cameras/import/preview", json={"csv_text": text})
        self.assertEqual(answer.status_code, 200, answer.text)
        rows = answer.json()["rows"]
        self.assertEqual([(row["row"], row["status"]) for row in rows], [(2, "invalid"), (3, "accepted")])
        self.assertIn("Camera name is required", " ".join(rows[0]["errors"]))


class WebhookUpdateTests(GrantCase):
    def test_an_explicit_empty_value_leaves_the_receivers_name_and_switch_as_they_were(self):
        created = self.admin.post("/api/webhooks", json={"name": "Operations receiver", "url": "http://192.0.2.5:8090/events", "events": ["recording.failure"],
                                                         "allow_private_receiver": True})
        self.assertEqual(created.status_code, 201, created.text)
        webhook = created.json()["webhook"]
        changed = self.admin.patch(f"/api/webhooks/{webhook['id']}", json={"name": None, "enabled": None})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual((changed.json()["webhook"]["name"], changed.json()["webhook"]["enabled"]), ("Operations receiver", True))
        renamed = self.admin.patch(f"/api/webhooks/{webhook['id']}", json={"name": "Night desk", "enabled": False}).json()["webhook"]
        self.assertEqual((renamed["name"], renamed["enabled"]), ("Night desk", False))  # A real change still applies


class AudioCheckTests(GrantCase):
    def test_an_audio_check_that_times_out_says_so_without_the_command_or_the_files_path(self):
        import subprocess
        from pathlib import Path
        from unittest import mock

        from app import exports

        path = Path(self.environment["NVR_RECORDINGS_PATH"]) / "a-private-folder" / "segment.mp4"

        def slow(command, *args, **kwargs):
            raise subprocess.TimeoutExpired(command, 15)

        with mock.patch.object(exports.subprocess, "run", slow), mock.patch.object(exports.shutil, "which", lambda name: "/usr/bin/ffprobe"):
            with self.assertRaises(RuntimeError) as raised:
                exports._has_audio(path)
        self.assertNotIsInstance(raised.exception, subprocess.TimeoutExpired)
        self.assertNotIn("a-private-folder", str(raised.exception))
        self.assertNotIn("ffprobe", str(raised.exception))
