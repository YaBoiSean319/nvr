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
