"""An AI detection behind a privacy mask is not sent to a webhook receiver (#376).

The owner's decision: unlike an alarm, which goes to the site's own staff, a webhook sends the data to another system, so what people are not
shown behind a mask is not sent there either. Rule events and alarms are unchanged."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import detection
from app.store import connect, initialize

AT = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
LEFT_HALF = [{"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0}]
_ENV = ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH")


class WebhookPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.temporary.name) / "credentials.key")
        initialize()
        with connect() as db:
            for camera in ("masked", "open"):
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,X'00')", (camera, camera))
            db.execute("INSERT INTO privacy_masks(camera_id,zones_json,camera_side,updated_at) VALUES('masked',?,'none',?)", (json.dumps(LEFT_HALF), AT.isoformat()))
            db.execute("INSERT INTO webhook_endpoints(id,name,target_url_ciphertext,signing_secret_ciphertext,events_json,enabled,created_at,updated_at) "
                       "VALUES('w','Receiver',X'00',X'00','[\"ai.detection\"]',1,?,?)", (AT.isoformat(), AT.isoformat()))

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def observe(self, camera_id: str, box: dict, label: str = "person") -> None:
        detection.record_observations(camera_id, [{"label": label, "confidence": 0.9, "box": box}], at=AT)

    def sent(self) -> list[tuple[str, str]]:
        with connect() as db:
            rows = db.execute("SELECT payload_json FROM webhook_deliveries WHERE event_type='ai.detection' ORDER BY created_at,rowid").fetchall()
        return [(json.loads(row[0])["camera_id"], json.loads(row[0])["label"]) for row in rows]

    def observations(self) -> int:
        with connect() as db:
            return db.execute("SELECT COUNT(*) FROM ai_observations").fetchone()[0]

    def test_a_detection_wholly_inside_the_mask_is_not_sent(self):
        self.observe("masked", {"x": 0.1, "y": 0.2, "width": 0.2, "height": 0.5})
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.observations(), 1)  # Still recorded on the NVR, where the masking rules for people apply

    def test_a_detection_that_touches_the_mask_is_not_sent(self):
        self.observe("masked", {"x": 0.45, "y": 0.2, "width": 0.2, "height": 0.5})
        self.assertEqual(self.sent(), [])

    def test_a_detection_clear_of_the_mask_is_sent(self):
        self.observe("masked", {"x": 0.6, "y": 0.2, "width": 0.2, "height": 0.5}, "vehicle")
        self.assertEqual(self.sent(), [("masked", "vehicle")])

    def test_a_camera_without_a_mask_sends_everything(self):
        self.observe("open", {"x": 0.1, "y": 0.2, "width": 0.2, "height": 0.5})
        self.assertEqual(self.sent(), [("open", "person")])

    def test_a_box_that_cannot_be_compared_with_the_mask_is_not_sent(self):
        self.observe("masked", {"x": 0.7, "y": 0.2, "width": None, "height": 0.5})
        self.assertEqual(self.sent(), [])

    def test_a_mask_the_camera_itself_applies_is_trusted_as_for_people(self):
        with connect() as db:
            db.execute("UPDATE cameras SET device_service_url='http://192.0.2.10/onvif/device_service' WHERE id='masked'")
            db.execute("UPDATE privacy_masks SET camera_side='applied',camera_masked_since='2026-11-01T00:00:00+00:00',written_to='http://192.0.2.10/onvif/device_service' WHERE camera_id='masked'")
        self.observe("masked", {"x": 0.1, "y": 0.2, "width": 0.2, "height": 0.5})
        self.assertEqual(self.sent(), [("masked", "person")])  # The camera blacks the area out itself: nothing behind it reaches the model


if __name__ == "__main__":
    unittest.main()
